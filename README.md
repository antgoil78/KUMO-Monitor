# KUMO Monitor

The maintained application is the current DEV/local Flask backend in `backend/`
and React app in `frontend/`. TST will use this same code with environment
configuration. Older application copies and completed legacy migration tools
are available in Git history; do not maintain parallel versions.

## Local configuration

Runtime secrets are loaded from `.env`. This file is ignored by Git and should
stay local to each developer machine.

Use `.env.example` as the non-secret template for required variables.

## Local PostgreSQL and native-task trial

The queue/stream implementation, including the working changes present before
this experiment, is preserved at commit `aacf516`, branch
`checkpoint/stream-queue-20261123` and tag `rollback/stream-queue-20261123`.
The maintained Monitor contains the complete DEV workflow/job catalog in
PostgreSQL. The [SPCS rollout](snowflake/spcs/README.md#postgresql-dev-rollout)
uses the same app and promotes PostgreSQL as the DEV engine. Its explicit
business-command flag enables the DEV SQL and DBT workflows; local disposable
SQL tests remain available with that flag disabled. TST continues to use its
selected service version and configuration.

### Complete PostgreSQL Monitor

Use **Workflow / Monitor → PostgreSQL · DEV workflows**. The existing table,
timeline, history and execution log read PostgreSQL. The complete catalog is
the `dev-workflow-catalog` graph's `catalog` JSONB value in
`kumo_task_engine.dependency_graphs`; executable graph nodes are derived from
that master. All workflow/job definitions, commands, assignments, dependency
rules and source schedule settings are retained. Unsupported definitions remain
visible with an explanation and disabled execution controls. Unassigned jobs
are shown in a separate group. Editing uses the same Workflow, Job and
Dependencies screens, with revision conflict protection and audit history.

For a fresh local database, from `backend/` after initialization:

```bash
# Explicit read of the three DEV application configuration tables; preview only.
python -m flask --env-file ../.env.postgres.local --app app task-engine import-catalog
# One-time promotion. Existing PostgreSQL WF1 edits and executions are preserved.
python -m flask --env-file ../.env.postgres.local --app app task-engine import-catalog --apply
```

Import never starts tasks or enables schedules and cannot overwrite an
installed master. It performs no Snowflake catalog or DDL writes. Existing
Snowflake history remains available through the Snowflake source; it is not
copied into PostgreSQL. Earlier PostgreSQL trial execution history remains
visible under the original source workflow IDs.

**Run workflow** is available on any supported workflow. Choose **Run only this
workflow** or **Run workflow and dependencies**. The latter includes downstream
children, never ancestors; with the current definitions, WF1 includes WF1–WF10.
A manual entry point does not require earlier parent outcomes. Downstream joins
still enforce their configured outcome/freshness rules, using exact parent
executions, including parents outside the selected subtree. Individual-job and
from-job starts remain unavailable. Queued, running or unknown executions of the
selected workflow, its direct parents, or its direct children disable Run in
the GUI and reject manual admission under the same PostgreSQL transaction lock.
This generic one-level rule also applies to scheduled and downstream admission;
more distant ancestors and descendants do not block execution. WF10 blocks only
WF8/WF9 (and itself); WF4 blocks WF2/WF3/WF5/WF6 (and itself). WF1 and then WF2/WF3
can run while WF10 is active; WF8/WF9 queue if WF10 is still active when reached.
Choosing dependencies still includes the complete downstream subtree.
Disabled workflows are omitted from new execution plans. A → disabled B → C
becomes A → C: C waits for A under A's upstream conditions, while B's outcome,
freshness and execution capabilities do not block the chain. Consecutive disabled
workflows are bypassed, and shared ancestors must satisfy the combined conditions
of every path to a join. Overlap protection uses these effective dependencies.
The decision log records the workflows omitted from each accepted request.
Configuration edits retain the frozen plans and running work of accepted groups.
A running execution remains visible
when a newer group also has a queued instance of the same workflow.
Independent workflows and sibling-only
runs can execute concurrently. Cancellation targets the accepted group and
waits for confirmation.
Configuration changes never rewrite an accepted group's snapshot. The DEV SPCS
business-command flag enables SQL and DBT execution. With that flag disabled,
local disposable SQL jobs retain the SELECT-expression guard. Unsupported
definitions remain visible with disabled execution controls.

The backend worker alone polls Snowflake's Information Schema `TASK_HISTORY`
for recorded outstanding native submissions, including native queue states.
It persists results in PostgreSQL, cleans up, then stops polling those tasks.
It continues admitted work without connected browsers. Browser status refreshes
read PostgreSQL every two seconds; timeline/history reads also use PostgreSQL.
PostgreSQL presence connections do not start the existing Snowflake cache,
dashboard or lock readers. Primary DEV also overrides stale Snowflake browser
presence, keeping those readers disabled. In a non-primary local trial, a separate
Snowflake-source client can still activate its existing readers. Missing feature discovery
does not silently select Snowflake. Authentication may resolve identity through
a warehouse-free Snowflake metadata connection; the header reports Snowflake
as **on demand** on PostgreSQL pages.

Background tabs close their realtime stream and reconnect when visible. This
leaves HTTP connection slots available for health and normal API requests when
several tabs share the same localhost origin. Admitted jobs continue in the
backend worker while tabs are hidden or closed.

Migration `005_catalog_monitor.sql` preserves existing runs, gives cascade nodes
stable view identities and replaces the graph-wide admission constraint with a
per-group constraint. Initialization applies only unapplied numbered migrations;
repeating it cannot recreate the superseded graph-wide index.

Migration `006_workflow_schedules.sql` links backend schedule occurrences to
catalog executions. Use the Monitor schedule menu to create, edit or pause a
workflow schedule. The existing `task-engine schedule` process admits these
occurrences through the dependency engine. A blocked occurrence remains
**QUEUED** until related executions finish; it retains its accepted snapshot
and does not expire while waiting. Parent outcomes/freshness rules still apply.
An already waiting instance is reused, so repeated due occurrences coalesce
instead of duplicating pending work. Completion commits wake-up work in the
same PostgreSQL transaction, including waiting executions in other groups.
Disabling a workflow pauses its schedule; accepted work retains its snapshot.
Imported schedules stay inactive until explicitly enabled. Standalone trial
schedules retain their earlier overlap policy.
The DEV catalog schedule migration registers every saved cron/timezone as a
paused backend schedule, including intent for disabled workflows. It preserves
existing PostgreSQL schedule edits, configuration revisions and execution history.
With the DEV engine environment configured, preview with
`PYTHONPATH=backend python backend/migrations/migrate_dev_postgres_schedules.py`;
apply with `--apply --expected-revision <reviewed catalog revision>`.
Rerunning it adds only missing workflow schedules and never enables them.
DBT model progress/DAG exploration, SQL conditions, definition
creation/deletion and assignment additions/removals remain outside this stage.

The existing Flask app accepts trial runs into PostgreSQL. A separate worker
from that same app creates one unscheduled Snowflake task per job attempt,
executes it, observes only outstanding submissions/executions, commits the
native outcome, then drops the task. Completed jobs are never polled again.
Cleanup has a separate bounded retry state and cannot change the job outcome.
The idle worker checks PostgreSQL once a second and opens no Snowflake session.
It does not depend on a logged-in browser to finish admitted work.

Admission freezes SQL, dependencies and timeouts in one PostgreSQL transaction.
A repeated request ID reuses the same run; a changed plan with that ID is
rejected. Submission ambiguity is persisted before `EXECUTE TASK`. After a
lost acknowledgment or worker restart, the worker checks that unique task's
history and never automatically executes the attempt again. Unresolved history
or changed native task identity becomes `UNKNOWN` after bounded recovery and
requires operator review. Dependants of an unknown attempt remain blocked.
There is deliberately no automatic retry/reset button for ambiguous attempts.

Create a local, ignored `.env.postgres.local` with permissions `0600`. It needs
`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB=kumo_engine_dev`,
`KUMO_TASK_PROTOTYPE_ENABLED=true` and a `KUMO_POSTGRES_DSN` connection URI for
that database at `127.0.0.1:55432`. Use a generated password and URI-encode it.
Keep the existing Snowflake credentials in `.env`. Do not commit either file.
The trial is restricted to DEV and `KUMO_DEV.MONITOR_APP.TMP_PG_ORCH_*` tasks.
Its default warehouse is `KUMO_ELT_GEN_1` and role is `KUMO_ADMIN`.

```bash
docker volume create kumo-monitor-postgres-dev
docker compose --env-file .env.postgres.local -f compose.task-engine.yml up -d
# Activate a Python environment with requirements.txt installed, then:
cd backend
python -m flask --env-file ../.env.postgres.local --app app task-engine init
python -m flask --env-file ../.env.postgres.local --app app task-engine work
# In another terminal, with the same Python environment and working directory:
python -m flask --env-file ../.env.postgres.local --app app task-engine schedule
# In another terminal:
python -m flask --env-file ../.env.postgres.local --app app run --host 127.0.0.1 --port 5000 --no-reload
```

Run the maintained frontend with `npm run dev` in `frontend/`. Open
`http://localhost:3000/?page=orchestrationMonitor` and select the PostgreSQL source.
Before complete-catalog promotion, **DEV task engine trial** in the Snowflake
source provides success, failure/skip, timeout, parallel and cancellation examples.
That temporary panel is hidden once the complete PostgreSQL catalog is installed.
Submissions and scheduled admissions require `KUMO_ORCHESTRATION_RUNS_ENABLED=true`
in the backend environment. Trial status/API reads use PostgreSQL; the monitor's
existing business-workflow readers still follow connected browser sessions.

Under **Trial schedules**, create a paused schedule, review its cron/timezone,
then enable it. The default `* * * * *` runs every minute: pause it after testing
if you do not want further warehouse executions. Editing supports the name,
cron, timezone and grace period; the copied example plan stays immutable.
Pausing prevents later admissions without cancelling an admitted execution.
Resuming starts with the next future occurrence and does not replay paused time.

Schedule intent (Enabled/Paused/Invalid), next planned time, scheduler health,
occurrence decisions and actual run outcomes are separate UI fields. Schedule
state and heartbeat refresh from PostgreSQL every five seconds while the view
is mounted, including between runs. A healthy scheduler heartbeat is at most
10 seconds old; 10–30 seconds is delayed and older heartbeats are unavailable.
An overdue schedule also reports delayed health. A worker with admission
disabled reports `EXECUTION_DISABLED`. Flask startup does not start either CLI
worker. They run independently of logged-in users; a future SPCS deployment
must keep its scheduler process available rather than suspending it with the UI.

Scheduling is PostgreSQL-only until work is admitted. The scheduler locks a due
schedule and records its occurrence, frozen run/attempts and next due time in
one transaction. A unique `(schedule_id, scheduled_for)` key prevents replay
across restarts or schedule edits. Schedule changes use expected revisions,
and creation uses an idempotent request ID. Changes and occurrence snapshots
are retained for review. `task-engine init` applies the incremental PostgreSQL
[schedule migration](backend/task_engine/migrations/002_schedules.sql); existing
runs and their history are preserved. No Snowflake DDL is needed.

The grace period is 0–3600 seconds, default 60. After downtime, at most the
latest due occurrence within grace is admitted; older occurrences are retained
as one `MISSED` time window, with an exclusive end. Nothing outside grace is
replayed. Zero grace requires exact punctuality and normally records a miss
with a periodically checking worker. Runs sharing a `workflowKey` admit only
one outstanding execution, including queued or unresolved `UNKNOWN` executions.
The UI assigns the same key to manual and scheduled copies of an example, and
competing schedules for that example share this guard. A due overlap records
`SKIPPED_OVERLAP` and advances the schedule. Calls omitting `workflowKey` retain
the first prototype's independent-run behavior. These example workflow keys
remain separate from the complete catalog's workflow identities.

Calendars use five numeric cron fields with lists, ranges and steps; day of
month and weekday use OR semantics when both are restricted. IANA timezone
names are stored alongside UTC due times. Missing local times during a daylight
saving transition are skipped; repeated local times run at their first
occurrence only. This policy is tested explicitly and is not an automatic
promise of identical behavior to every existing native Snowflake schedule.

### WF1 workflow dependency trial

Before complete-catalog promotion, the **Workflow dependency trial** section tests
`WF1 → (WF2, WF3) → WF4` in PostgreSQL. Use its **Run WF1** button; the existing
workflow controls farther down the page still use their current Snowflake
engine. WF5–WF10 are outside this snapshot and are not released by it.

The imported SQL is preserved: WF1 waits 20 seconds, WF2 30, WF3 60 and WF4 20.
Both children are admitted in one PostgreSQL transaction before either native
task can start. WF4 waits for both parent executions. The UI shows the actual
workflow outcomes, waiting/blocking reasons, the pinned parent run IDs and links
to each workflow's job details. Source workflow/job IDs and per-parent result,
ALLOW and freshness rules are retained. WF4's current WF2 freshness flag is true;
its WF3 flag is false. Native query start and completion times are used for
freshness, so delayed observation does not make an old parent appear recent.

Import is explicit and read-only against Snowflake. From `backend/`:

```bash
python -m flask --env-file ../.env.postgres.local --app app task-engine init
# Preview: exactly the four named workflows, their rules and assigned jobs.
python -m flask --env-file ../.env.postgres.local --app app task-engine import-wf1
# Save that validated snapshot into PostgreSQL; no Snowflake catalog writes.
python -m flask --env-file ../.env.postgres.local --app app task-engine import-wf1 --apply
```

The initial import makes PostgreSQL the master for this trial. Subsequent
imports cannot overwrite a changed master, even with `--expected-revision`;
use the configuration screens described below. Already accepted executions
retain their complete graph and job snapshot. The importer validates the
four-workflow diamond and accepts only
enabled SQL jobs that pass the existing SELECT-expression restrictions. It
rejects unsupported job gates, SQL conditions, dbt jobs, mixed trigger policies
or external parents instead of silently omitting them. Only configuration for
these four workflows is read; no live account inventory or DDL export is used.

### PostgreSQL master configuration for WF1–WF4

The existing **Workflow**, **Job** and **Dependencies** pages offer a
**Configuration source** selector when the DEV task prototype is enabled.
Select **PostgreSQL · DEV workflows** (the default). Before complete-catalog
promotion this edits the WF1 trial; afterward it edits the complete catalog.
The Snowflake selection continues to address the existing engine's catalog;
these are distinct execution scopes and changes are never mirrored between them.
The source choice persists for this browser tab. A PostgreSQL error leaves that
source selected and never redirects a save to Snowflake.

The graph's JSONB definition in `kumo_task_engine.dependency_graphs` is the
master, rather than an additional catalog copy. The Flask backend adapts it to
the current editors. It supports workflow name/group/description, enabled state,
sequential/parallel job execution, SQL job name/layer/description/text/timeout/
enabled state, reordering existing jobs, and adding/removing parent workflow
rules with independent outcome, ALLOW and freshness settings. Object identities
remain stable when names change. Creating/deleting definitions, adding/removing
job assignments, graph schedules, external triggers, SQL conditions, business
table SQL and non-SQL job types remain outside this first configuration stage.

Every save checks the loaded graph revision, validates the whole graph, then
commits the new definition and a full audit revision in
`kumo_task_engine.graph_revisions` together. A concurrent edit returns HTTP 409;
close the editor and refresh before reapplying the draft. The screens display
the current revision and recent configuration history. Run admission uses the
same PostgreSQL transaction lock and freezes the graph revision, so edits to
waiting children cannot change an already accepted execution. No save executes
SQL, creates tasks, or changes existing Snowflake configuration. Job previews
and legacy execution-status requests are suppressed in the PostgreSQL editors.

Disabled SQL jobs create no native attempts. An enabled job retains its enabled
prerequisites through any disabled jobs. A workflow with no enabled jobs is
SKIPPED, and dependent workflows remain blocked by that skipped outcome.
Run the full trial from **Monitor → Workflow dependency trial → Run WF1**;
individual workflow/job runs are deliberately unavailable in the configuration
view. Migration `004_master_configuration.sql` promotes the installed WF1
graph and records its baseline without changing past or active executions.

Each trial creates a frozen execution group with one run per workflow. Parents
must belong to that group; an earlier trial's success cannot satisfy a new
trial. This first stage allows one outstanding group per graph, including any
unknown outcome. Together with reserved workflow keys and parent completion
gates, this prevents a newer parent/ancestor from overlapping a child from an
older trial. It is a deliberately narrower policy than a general engine with
overlapping generations. There is no partial-run endpoint or recurring
dependency-trial schedule in this stage.

The existing `task-engine work` process handles the PostgreSQL dependency outbox
and native task worker; no additional daemon is required. Parent outcomes and
their pending dependency work commit together. Dependency processing cannot
change a parent's successful result. Evaluation/admission failures roll back
the entire child frontier, retry at most six times, then expose a dispatch
error. **Retry dependency dispatch** only retries admission decisions; existing
native attempts are never reset or resubmitted. Duplicate notifications and
worker replacements reuse the same per-group, per-workflow request identity.
Unknown outcomes remain blocked for review. Cancelling a group stops pending
admissions and requests cancellation of active native work, waiting for native
confirmation before reporting completion.

The PostgreSQL Monitor shows exhausted dependency-dispatch errors separately
from native job outcomes. Each active error group has an alert with its workflow
names, request time, error and attempt count, plus **Retry dependency dispatch**.
The alerts remain visible across table filters and the timeline, including when
the workflow row displays an older running instance. Waiting workflows explain
that dispatch is paused; completed and running jobs keep their actual statuses.
Worker heartbeat health is displayed independently of paused dispatch groups.
Retry queues the existing group for dependency evaluation and retains all
recorded native attempts. It does not restart successful/running jobs or resolve
a native UNKNOWN outcome. The retry button follows the existing execution
controls, and a rejected retry leaves the error visible. Waiting History and
Log entries include the dispatch error explanation. These reads and the retry
request use PostgreSQL only; native task polling is unchanged.

Dependency evaluation, waiting states, dispatcher health and idle checks use
PostgreSQL only. SQL/data-readiness predicates still need their own bounded
evaluation and wake-up design; they are not polled by this trial. Native SQL
tasks currently produce success, failure or cancellation, not custom warning
outcomes, although warning rule flags are retained for later migration.

For catalog schedules, an existing queued workflow can fulfill a due occurrence
only when its frozen group contains every requested downstream workflow and
uses the current catalog revision. A manual workflow-only run cannot absorb a
schedule requiring descendants. The scheduler creates a separate queued group
and leaves the manual snapshot intact; the normal overlap rules determine when
each workflow can start. Compatible queued groups still share due occurrences.
Groups with cancellation already requested are excluded from reuse, including
their waiting descendants. A newly due occurrence gets a separate group and
retains its own cancellation state. It waits for conflicting native work to
finish cancellation before starting, and its normal parent outcome rules still
apply. Occurrences accepted into a shared group before cancellation was
requested remain part of that shared group. Rejected reuse is recorded as
`PENDING_CANCELLATION_EXCLUDED` in the PostgreSQL decision history.

Migration `007_dependency_decisions.sql` adds the PostgreSQL-only
`kumo_task_engine.dependency_decisions` history. It records admission/reuse and
incompatible scopes, rejected requests, parent outcome/freshness checks, related
execution blockers, workflow admission/wait/block/skip/cancel decisions, group
states, wake-ups, dispatch retries/errors, and due/missed/invalid schedule
decisions. Rows include timestamps, group/workflow/run and schedule identities,
configuration revision, an evaluation correlation ID and JSONB reasons where
applicable. Decisions commit with their state changes. After a failed evaluation
rolls back, only its error/retry record is written in a fresh transaction.
Rejected manual requests are also recorded after admission rolls back.

Idle and execution-disabled heartbeats add no decision rows. The history stores
identities, rules and states without SQL commands or raw database exception
text, and does not query Snowflake. It starts when this migration and code are
installed; it does not reconstruct earlier decisions. Rows remain available
after execution records are removed; no automatic history pruning is configured.
There is no new Monitor view or API for this history yet. Inspect recent rows
directly in PostgreSQL, optionally filtering by `cascade_id` or `schedule_id`:

```sql
SELECT id, decided_at, component, decision, cascade_id, node_key, run_id, facts
FROM kumo_task_engine.dependency_decisions
ORDER BY id DESC
LIMIT 100;
```

The worker CLI also provides `task-engine status` and `task-engine submit
PLAN.json`. Plans contain a stable `requestId`, optional `workflowKey`,
`SEQUENTIAL` or `PARALLEL` mode, and jobs with `key`, `sql`, optional `timeoutSeconds` (1–300)
and `dependsOn` (preceding keys only). SQL is restricted to single SELECT
expressions from a small function allowlist. Business table reads, DML, CALLs
and dbt execution are excluded from this prototype. Leases prevent concurrent
claims; the PostgreSQL implementation uses one transaction lock for this small
trial, so it is not a throughput benchmark. Cancellation is given priority
over queued submissions, and requires native confirmation. A job that has
already finished retains its actual outcome when cancellation arrives late.

Temporary Snowflake connection failures (DNS, connection loss and transport
timeouts) retain the accepted attempt and retry automatically with exponential
backoff, capped at five minutes. PostgreSQL stores the recovery reason, first
failure time and next retry time, so restarting the worker does not lose the
retry. Recovery only runs for recorded work or pending cleanup; an idle worker
does not probe Snowflake. Authentication, permissions, certificate failures and
task identity drift retain bounded retries or operator review.

Before submission, recovery reuses and validates the same temporary task. Once
the durable submission fence has been recorded, recovery only observes its
history and never repeats EXECUTE TASK. Successful connectivity clears the
network retry count. If history responds but no invocation can be established,
the separate six-check ambiguity limit still stops for review. Cleanup outages
retain the committed workload outcome and retry cleanup without polling a
completed job's status.

The PostgreSQL Monitor separates **Status** from **State**. Status, Last Run,
Duration, job results, progress and Log all describe the latest actual execution;
**NO RUN** means no native execution has started. A newer pending or blocked
request never replaces that execution's SUCCESS or FAILED result. State describes
planned work: **WAITING(n)** counts waiting execution requests for the workflow,
**QUEUED** means admitted work awaiting native execution, and **BLOCKED** identifies
a stopped dependency plan. **SCHEDULED** indicates an enabled backend schedule;
Next Run shows its next occurrence. Status and State have separate filters.
RUNNING is shown only in Status; State stays blank unless there is additional
planning information, such as waiting requests or an enabled schedule.
State shows only direct parent names and statuses, with full reasons on hover.
Child overlap checks still apply; Run controls explain those exclusions. Waiting
and queued counts remain separate even while an execution runs.
History and Log retain each request's lifecycle, including requests that never
started. This display split does not change dependency admission or reopen a
stopped plan; use the existing Retry or Run actions as appropriate.

For a confirmed SQL failure, the Monitor's **Retry** action retries the failed
workflow and restores its unexecuted blocked descendants to **WAITING**. The
failed workflow uses its current job configuration, so a repaired SQL command
can run; descendants retain their accepted dependency rules and job plans.
Successful or still-running parents keep their original execution bindings.
Completed workflows are not rerun, and cancelled or superseded branches are not
resumed. The normal **Run** action remains a new execution with the chosen scope.

Recovery creates a new execution group linked to the original nodes. Failed and
blocked history remains unchanged. PostgreSQL records each retry and resumed
workflow, including its actor and source execution. Concurrent retries of the
same failure reuse one durable request; direct parent/child exclusions still
apply. This path is separate from connection recovery and never retries UNKNOWN
outcomes as new work.

The Monitor shows automatic recovery and the next retry time. Safely recoverable
UNKNOWN attempts expose **Recover**, including older task-creation failures.
This queues the same attempt: pre-submission failures resume creation, and
submitted attempts only resume observation. Task drift cannot use this action.
Cancellation remains in force during recovery. The API uses the trusted actor,
honours execution controls and records recovery requests and connection retry
decisions in PostgreSQL. Migration `008_connection_recovery.sql` is PostgreSQL
only; apply it with `flask --app app task-engine init` using the configured DEV
environment before restarting the API and worker.

`backend/tests/test_postgres_connection_recovery.py` simulates long DNS/connection
outages, lost create/execute acknowledgements, worker restart, cancellation,
cleanup failures, thirty concurrent recovery requests, and WF1–WF10 completion
after WF4 recovers. It also checks that missing history remains bounded and no
native work is executed twice.

For automated PostgreSQL tests, create a separate database ending in `_test`
and set `KUMO_TASK_ENGINE_TEST_DSN` to it. The tests truncate only that isolated
database. `backend/tests/test_postgres_multiuser.py` adds 30-user coverage using
the maintained Flask app on an ephemeral loopback port and real PostgreSQL.
Only Snowflake identity resolution and native outcomes are simulated. It checks
distinct per-user attribution, simultaneous starts and request replays, direct
neighbor exclusions, independent runs, competing configuration revisions,
concurrent cancel/retry/start requests, and realtime disconnect/reconnect with
no new identity lookup. Cancellation and dispatch retry HTTP actions retain the
trusted request actor in the PostgreSQL decision history.

The load case keeps 30 SSE streams open while every user polls a synthetic
26-workflow/51-job Monitor every two seconds. It also reads History and health,
advances simulated work, checks final status visibility for every user, verifies
presence returns to zero, and rejects any Snowflake connection or bootstrap.
It reports latency percentiles and application PostgreSQL connection peaks.
The default load window is ten seconds; use sixty seconds for a longer check:

```bash
# Set KUMO_TASK_ENGINE_TEST_DSN to the separate _test database first.
KUMO_MULTIUSER_LOAD_SECONDS=60 python -m unittest discover -s backend/tests -p 'test_postgres_multiuser.py' -v
```

These are single-process HTTP tests, not a browser-rendering or SPCS ingress
capacity test. The current PostgreSQL Monitor uses one snapshot request per
browser every two seconds; thirty open Monitors produce about fifteen requests
per second. SSE streams retain no PostgreSQL connection. Exact response times
depend on hardware, history volume and concurrent workloads; use the reported
percentiles when comparing them with the polling interval.

`backend/tests/test_postgres_dependency_scenarios.py` also exercises the WF1–WF10
joins and direct-neighbor exclusions. It checks simultaneous WF2/WF5 and
WF2/WF3/WF5 entry runs, all six parent/child completion orders for a queued WF4,
WF5's downstream progress, cancellation confirmation, unknown outcomes, and
missing, failed or stale external parents. The fixture preserves DEV's parent
freshness flags. Monitor, History and Log name the blocking parent or child and
its actual state; an idle workflow whose Run button is disabled stays NOT RUN.
Blocker labels refresh from PostgreSQL between dispatcher evaluations. A
finished blocker shows "Awaiting dependency evaluation" until the scheduler
evaluates it; historical terminal decisions retain their recorded outcomes.
Normal waits are displayed without error styling; unknown outcomes need review.

Two acceptance cases explicitly use `expectedFailure`: starting WF2/WF5 or
WF2/WF3/WF5 with downstream workflows included on every request. The current
engine reserves all future nodes, so a later independent entry may be rejected
by an earlier group's waiting descendant. Missing external parents are also
pinned at admission and do not bind to later requests. The passing workflow-only
and scheduled-WF4 checks do not establish that these full-scope cases work.
These tests keep the unresolved behavior visible without changing reservation,
parent-binding, freshness or execution rules as part of the status work.

The opt-in `backend/tests/live_postgres_task_engine.py --run` performs
disposable DEV native-task tests and a 90-second idle check. Load the local
environment files before invoking it; the script does not read secrets files.
Its targeted report is written to `review/`. Generated tasks are excluded from
saved Snowflake DDL.

The opt-in `backend/tests/live_postgres_schedules.py --run` waits for a real
minute boundary, admits and executes a disposable native task, replaces the
scheduler identity after admission, then checks 90 seconds with an enabled
future schedule and no Snowflake calls. Stop other trial workers while it
drives its own worker. It pauses its own fixture schedules on exit.

The opt-in `backend/tests/live_postgres_dependencies.py --run` executes the
imported WF1 diamond through real native tasks, checks actual sibling overlap
and the two-parent join, replaces its dependency worker after parent completion,
replays dependency delivery without new native submissions, and proves 90
seconds of idle operation without Snowflake calls. Stop other trial execution
workers while it drives its own worker. It never starts the old WF1 engine.

To return to the saved queue code, pause all trial schedules, stop the scheduler,
stop admitting trial runs, let the execution worker
finish and confirm that there are no active, `UNKNOWN`, or pending/error
cleanup attempts or outstanding dependency groups/dispatch errors. Stop the local worker/API, commit or stash trial changes,
then `git switch checkpoint/stream-queue-20261123` and restart the existing app
without the trial environment file. The tag remains available even if the
checkpoint branch moves. Git switching does not cancel native executions or
undo PostgreSQL state. Retain the PostgreSQL volume until any unresolved
attempts have been reviewed; do not reset them to queued.

The [DEV SPCS rollout](snowflake/spcs/README.md#postgresql-dev-rollout) hosts this
same backend in SPCS, with a separate persistent PostgreSQL service and an
independent PostgreSQL image workflow. `KUMO_PG_BUSINESS_JOBS_ENABLED=true`
enables frozen business SQL and DBT commands in native tasks; the local disposable
SQL trial remains the default when that flag is false. DEV releases do not
change TST services or enable legacy scheduled maintenance.

Validated on 2026-11-23: 348 backend tests (including 30 prototype tests with
real isolated PostgreSQL where applicable), three new browser tests, frontend
production build, and the actual localhost panel. The
[native DEV test report](review/dev-postgres-task-engine-20261123-133049.json)
covers success, failure/dependent skip, timeout, confirmed cancellation,
parallel overlap and lost-acknowledgment recovery after worker replacement.
All disposable tasks were absent after cleanup. A 90-second idle window made
87 PostgreSQL checks, zero Snowflake adapter calls and zero prototype-tagged
Snowflake queries. This proves the prototype worker's idle behavior, not the
shared warehouse's suspension while other services or users are active.

The schedule extension passed 376 backend tests (58 prototype tests, with real
isolated PostgreSQL where applicable), seven browser tests and the frontend
build. The [live schedule report](review/dev-postgres-schedules-20261123-135257.json)
records a real cron boundary, one native submission, successful cleanup and a
90-second idle window with an enabled future schedule: 84 combined scheduler /
execution-worker checks, zero Snowflake calls and zero prototype-tagged queries.
The [localhost UI report](review/dev-postgres-schedules-local-ui.json) covers
creation, enablement, real cron admission, pausing future work and successful
completion/cleanup of two dependent jobs. All test fixture schedules were left
paused. The local scheduler and execution worker remain available for testing.

The WF1 dependency extension passed 400 backend tests, 12 browser tests and the
frontend build. The [native WF1 report](review/dev-postgres-dependencies-20261123-142139.json)
records four successful native SQL runs, real WF2/WF3 overlap, WF4 starting only
after both parents finished, replacement of the dependency worker before child
admission, and replay without another native submission. All four temporary
tasks were absent after cleanup. The following 90-second idle window made 84
combined dependency/execution-worker checks with zero Snowflake calls and zero
prototype-tagged queries. The [localhost view check](review/dev-postgres-wf1-local-ui.json)
verified the four outcomes, mixed WF4 freshness flags and job-detail navigation
against PostgreSQL without creating another execution. The WF1 snapshot is
installed for manual testing; it has no recurring schedule.

The master configuration extension passed 416 backend tests, including 14 new
tests with isolated PostgreSQL, 16 relevant browser tests, 33 frontend unit
tests and the frontend build. The [localhost configuration check](review/dev-postgres-master-configuration-local-ui.json)
saved/reopened/restored a workflow description and saved/restored a SQL job
description against the actual backend, leaving SQL, enabled flags, timeouts
and parent rules unchanged. These four saves are retained in revision history.
The [native execution check](review/dev-postgres-master-configuration-native.json)
then ran revision 5: four successful SQL workflows, overlapping siblings,
WF4 after both parents, one acknowledged submission per workflow, and all
temporary-task cleanup complete. See the [validation summary](review/dev-postgres-master-configuration-validation.json)
for scope and remaining limitations. No existing Snowflake catalog, static DDL,
TST or SPCS service was changed.

The complete-catalog Monitor extension was checked against 434 unique backend
tests across the full and final targeted runs, 42 unique browser tests and 34
frontend unit tests; the frontend build passed. The
[catalog promotion report](review/dev-postgres-monitor-catalog.json) records all
26 workflows and 51 jobs, with ten executable SQL test workflows and 16
workflows requiring further execution support. No workflows were started by
the import or Monitor verification, and no graph schedules were enabled.
The [live localhost check](review/dev-postgres-monitor-local-ui.json) kept two
real browser sessions connected for 90 seconds: 98 PostgreSQL Monitor reads,
zero Snowflake database events from the API, zero Snowflake connections from
the idle execution worker, and all three existing Snowflake background readers
disabled. Existing PostgreSQL history/log navigation and all 26 dependency
configuration groups were also checked. See the
[validation summary](review/dev-postgres-monitor-validation.json) for coverage
and remaining scope. No Snowflake static DDL, existing configuration, TST or
SPCS service was changed.

## DEV/TST configuration and migration

Set the full parameter table path when starting the service:

```dotenv
KUMO_ENVIRONMENT_TABLE=KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS
KUMO_ENV=DEV
```

`KUMO_ENVIRONMENT_TABLE` is required for a connected service. The table can live
in any accessible database/schema and can have a different name. The app reads
active, non-secret `STRING` parameters for instance `DEFAULT`, group
`ENVIRONMENT`, before loading its repositories and pages. Missing or invalid
required parameters stop startup rather than routing queries to a fallback.
Mock mode keeps local defaults without connecting.

| Object group | Location from parameters |
| --- | --- |
| Workflow procedures and tasks | `[DATABASE].[MONITOR_APP_SCHEMA]` |
| Monitor configuration, workflow/job catalog, audit, queues and history | `[DATABASE].[META_SCHEMA]` |
| LIM loader, raw file metadata and both readiness logs | `[DATABASE].[META_SCHEMA]` |
| Stage, file format, batch control, progress and statuses | `[DATABASE].[APP_SCHEMA]` |
| RAW subject data | `[DATABASE].[RAW_SCHEMA]` |
| dbt project object | `[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]` |
| dbt profile target | `[DBT_TARGET]` (case preserved) |

The same app image supports different database and schema names. `KUMO_ENV`
sets the DEV/TEST label and mock defaults; it does not override the parameter
store's database. `KUMO_DEPLOYMENT_ENV` can label future services UAT or PROD.
The top bar shows the backend's environment label, and `/api/health` reports
both the parameter-table address and resolved object context.

The app loads routing at startup. Restart it after changing routing parameters
or the bootstrap table path. Old `KUMO_DB`, schema and per-object overrides must
be removed or agree with the parameter store. Snowflake-provided service
`SNOWFLAKE_DATABASE`/`SNOWFLAKE_SCHEMA` do not control application routing.
Credentials, account, roles and warehouses remain deployment settings.

In SPCS, put `KUMO_ENVIRONMENT_TABLE` under `spec.containers[].env`. The offline
[`render_service.py`](snowflake/spcs/render_service.py) accepts
`--environment-table DATABASE.SCHEMA.TABLE`; it only prepares deployment SQL.
The Snowflake service object itself has its own concrete deployment address.
See the [SPCS deployment guide](snowflake/spcs/README.md).

The Snowflake engine reads the same parameter table through
`SP_GET_ENVIRONMENT()` for each invocation. Its bootstrap pointer and the master
stream condition are bound by
[`apply_environment_routing.py`](backend/migrations/apply_environment_routing.py).
Run that migration against the new parameter table when moving the engine or
its configuration store. Native DDL object names and task conditions must have
concrete addresses at deployment; table access and execution routing inside
the engine use the parameters. The migration preserves grants and the master's
original running/suspended state, and updates only changed DDL copies.

TST provisioning remains a separate step: provision its objects and parameter
rows, bind its engine to that table, and start the same app image with that
full table path. No workflow/job definition edits are needed.

Workflow SQL and dbt fields use `[VARIABLE]` tokens from **Settings → Environment**.
For example, `[DATABASE].[MONITOR_APP_SCHEMA]` locates workflow procedures;
`[DATABASE].[APP_SCHEMA]` locates data-platform metadata. Use `'[run_id]'` for a
SQL string containing the current run ID. The command preview resolves the
current editor/run-dialog values without running SQL or dbt. See the
[command template guide](snowflake/README.md#portable-workflow-commands).

Account, warehouses, roles and image location are deployment resources.
SPCS uses Snowflake's service OAuth token; the service owner needs access to
the selected environment, image repository, compute pool and warehouses.
The old account role named `KUMO_ADMIN` is independent of the database with
that name. The SPCS renderer sets `KUMO_LIM_ROLE` to the selected service owner
for File Ingestion, without embedding user credentials.

Saving or enabling a workflow schedule resumes its Snowflake scheduler when
the workflow is enabled. Disabling the schedule removes its scheduler. The
former `KUMO_AUTO_RESUME_TASKS` setting is ignored, including in existing service
specifications. Starting the app does not activate copied schedules or resume
the persistent master dispatcher. Generated execution tasks inside procedures
retain their own lifecycle.

In environments retaining the old engine (currently TST), use **Settings → Engine** to inspect, resume or suspend the persistent
`TASK_WF_MASTER_DISPATCHER` in the configured engine namespace. Resuming allows
queued runs to dispatch, including existing requests. Suspending pauses new
dispatches while work already running can finish; individual workflow schedules
are managed separately. Both actions use the app's service context and are
audited against the browser user. The service role needs OWNERSHIP or OPERATE
on the task; Snowflake also checks the task owner's execution privileges when
resuming. Status refreshes every 15 seconds while the Engine tab is open.
Demo mode simulates the controls; an unconfigured Snowflake connection disables
them. App startup never resumes the master task automatically.

DEV has 27 existing execution workflows. The new catalog was rebuilt from
26 of them, excluding `RUN DEPENDENCIES`: 14 workflows, 38 jobs and 52 dependency
rows. Each subject's readiness, SDL and EDV steps form one `… LOAD` workflow;
`LOAD RAW DAILY` and `MAINTENANCE - CLEANUP` each contain one job.
Workflow/job command fields use parameter templates; DEV parameter values resolve
to the `KUMO_DEV.DBT_APP.KUMO_DEV` project and `kumo_DEV` target. Copied schedules are disabled and the dispatcher is suspended.
Workflow execution validation remains; `META.WORKFLOW_DEP_RULE` is intentionally
not created yet. The DEV SPCS service has been deployed as
`KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE`, owned by `KUMO_ADMIN`, using image
`build-215-1-022ffedd2bf2`. TST provisioning remains a later step.
The shared image repository and compute pool have been created using
[create_shared_infrastructure.sql](snowflake/spcs/create_shared_infrastructure.sql).
The maintained DEV migrations and app definitions are documented in
[snowflake/README.md](snowflake/README.md).

CI publishes a separate image for every build and rerun, tagged
`build-<run-number>-<attempt>-<12-character-commit>`, for example
`build-123-1-a1b2c3d4e5f6`. It does not push a moving `latest` tag. The version
is also baked into the image for the sidebar and `/api/health`. The Actions
summary shows both the complete Docker image URI and the SPCS image path.

The default repository is
`<registry>/kumo_shared/monitor_app/monitor_repository/kumo-monitor`.
Optional GitHub repository variables `KUMO_IMAGE_REPOSITORY` (that full path
without a tag) and `SNOWFLAKE_REGISTRY` (registry hostname) can override it.
The older `KUMO_IMAGE_URI` variable remains a fallback: its repository is reused,
but any old tag is replaced with the generated version. The registry hostname
must match the repository path. These variables are not needed with the defaults.

Choose an exact published version in each service specification. DEV can run
a newer build while TST keeps an earlier build; promote the same image tag to
TST after verification. Publishing alone does not update any service. UAT/PROD
will use repositories and pools in their own accounts.

## Local Docker run

After Docker is installed:

```bash
docker compose up --build
```

Open:

```text
http://localhost:5000
```

`docker-compose.yml` automatically reads `.env` and passes the Snowflake
settings into the container at runtime. Do not pass Snowflake credentials as
Docker build args or commit them to Git.

### Image version

The image tag and the version displayed in the sidebar use
`KUMO_IMAGE_VERSION`. CI sets it to the generated build tag. Local Docker builds
default to `latest`; to build and run a specific local version:

```bash
KUMO_IMAGE_VERSION=1.2.0 docker compose up --build -d
```

This creates `kumo-monitor:1.2.0`, exposes `imageVersion` from `/api/health`,
and displays `1.2.0` in the application sidebar. You can optionally provide
`GIT_SHA` alongside it to expose the source revision in the version tooltip.

### Snowflake account identifier

If Snowflake returns an error like:

```text
404 Not Found: post <account>.snowflakecomputing.com:443/session/v1/login-request
```

then `SNOWFLAKE_ACCOUNT` is probably not the full Snowflake account identifier
for the account's region/cloud.

Use the value shown in Snowsight under account details for connectors/drivers.
For the Python connector, use the account identifier without `https://` and
without `.snowflakecomputing.com`.

Examples:

```bash
SNOWFLAKE_ACCOUNT=myorg-myaccount
SNOWFLAKE_ACCOUNT=abc12345.west-europe.azure
SNOWFLAKE_ACCOUNT=abc12345.eu-central-1.aws
```

### MFA for local password auth

If Snowflake requires MFA for your user, set a current TOTP code before starting
the container:

```bash
SNOWFLAKE_PASSCODE=123456 docker compose up --build
```

The passcode is short-lived. Do not commit it, and do not rely on this pattern
for unattended deployments. For automated builds/deployments, prefer a
deployment-specific auth method such as Snowflake-managed service auth,
key-pair auth, OAuth, or a programmatic access token according to your Snowflake
security policy.

## Local development run

### Checks before opening a PR

Run these checks from the repository root after installing the backend and
frontend dependencies. Use Node 22, matching the image builder and PR CI:

```bash
KUMO_USE_MOCK=true python -m unittest discover -s backend/tests -p 'test_*.py'
npm --prefix frontend test
npm --prefix frontend run build
docker compose --env-file .env.example config --quiet
cd frontend
npx playwright install chromium
npm run test:e2e
```

The browser suite starts its own Vite server on port 3100 and intercepts API
requests with fixtures. It needs no backend, Snowflake credentials, or running
workloads. It checks timeline scrolling, relationship lines, selected-run
actions, history retention across refresh/navigation, and dependency draft
protection. On machines with a system Chrome installation, you can use
`PLAYWRIGHT_CHANNEL=chrome npm run test:e2e` instead of installing Chromium.
Pull requests to `main` run these checks in `validate-pr.yml`.
That workflow does not publish images or change a service; the existing image
workflow continues to publish on merges to `main`.

The DEV engine dispatches eligible downstream workflows after completion,
including joins with multiple parents and duplicate-request protection. DEV
completion emails are restored through the current workflow catalog; see the
[notification migration guide](snowflake/README.md#workflow-notifications-in-dev-and-tst).
dbt warning/artifact classification remains a legacy parity gap. Before promoting
the engine for workloads relying on it, complete its integration and DEV validation.
The TST/ACC notification migration enables failure emails for every workflow;
DEV restores existing notification rules only. Fixture
browser tests also do not replace live DEV checks for accepted run IDs, task permissions, workload
execution, and schedule controls. See the
[engine validation instructions](snowflake/README.md).

### Start the application

Backend:

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r ../requirements.txt
python -m flask --app app run --host 0.0.0.0 --port 5000
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Open:

```text
http://localhost:3000
```

The Vite dev server proxies `/api` to the Flask backend on port `5000`.

## Development workflow

1. Code and test locally using Docker or the split backend/frontend dev servers.
2. Keep `.env` local only; commit `.env.example`, code, and docs.
3. Push changes for review and merge to `main`.
4. Let `.github/workflows/deploy-image.yml` build and publish the image on merge.
   Routine work does not manually dispatch the workflow or update SPCS directly.

## Snowflake app definitions

Current table, procedure and persistent task definitions are stored under
[`snowflake/`](snowflake/README.md), together with supporting views, functions,
streams and file formats, an object inventory, a restore order and a repeatable
export script. Generated
workflow scheduler tasks and temporary workflow run tasks are excluded.

DDL copies are maintained only for DEV's `MONITOR_APP` and `MONITOR_META`.
When we change an object's DDL, update that object's saved definition and any
applicable migration. Routine app work and configuration-row changes do not
require an export. Refresh both schemas only when explicitly requested; see
[`snowflake/README.md`](snowflake/README.md) and [`AGENTS.md`](AGENTS.md).

## Orchestration access in SPCS

The Workflow, Job and Flow pages read the selected environment's `MONITOR_META`
schema. Local runs use
the configured `SNOWFLAKE_ROLE`, while SPCS service connections use the role
that owns the service. The Orchestration endpoints use service credentials,
as the existing admin pages do, even when a browser caller token is present.
Users with access to the application can view and edit this shared catalog.
Caller credentials continue to identify users for sessions and request logging;
`CURRENT_USER()` in catalog table audit columns records the database service user.

When provisioning TST, grant its service owner access to that environment's
database, `MONITOR_APP` procedures/tasks, and `MONITOR_META` tables, including
the table reads/writes used by the catalog. Prepare grants for the selected
environment rather than replaying the retired legacy grant migration.

Deploy the updated backend image so these endpoints use the service role.
In the service specification, leave `SNOWFLAKE_ROLE` unset to use the service
owner by default, or set it to that owner role. The owner also needs `USAGE`
on the configured `SNOWFLAKE_WAREHOUSE`, as for the other admin pages.

After deployment, check `/api/orchestration/catalog` and
`/api/orchestration/flow` from the signed-in application. If an object cannot
be found, check the `orchestrationNamespace` reported by `/api/health` and
verify that `WORKFLOW`, `JOB` and `DEPENDENCIES` exist
there and are accessible to the service owner.

**Workflow → Workflow** administers these three tables using the same
expandable workflow/job layout as the Monitor reference. Create or edit
workflow and job definitions, assign existing jobs, and drag jobs or use the
arrow buttons to change their order. Save changes to persist assignments and
parent links together. The first job's `PARENT_ID` is its workflow's ID; each
following job points to the preceding job. Existing status, SQL and ALLOW rules
are preserved when reordering. A job can be reused across workflows.

The page reports unmatched dependency references without deleting them or
importing assignments from `WORKFLOW_DEPENDENCY`. It does not change the
existing execution engine, schedules or tasks.

The parallel execution engine uses `WORKFLOW_EXECUTION_LOG` for both pending
requests and workflow/job results. Each manual run creates one temporary
workflow task bound to that execution and removes it afterward. Idle unscheduled
workflows have no task; scheduled workflows keep a persistent scheduler. Jobs
never own tasks. Workflow and Monitor pages both include Run controls.
Job Run offers **Run only this job** and **Run from this job**. Definitions,
conditions and commands are snapshotted when the request is accepted.

Workflow Edit includes **Job execution**: **Sequential** (the default) or
**Parallel** for the whole workflow. Parallel starts all selected jobs
concurrently inside the same workflow task, without job-to-job result gating.
Enabled/ALLOW flags and each job's SQL condition still apply. A failed or
timed-out parallel job does not stop its siblings; the workflow waits for every
selected job and fails if any failed. The mode is frozen with the run, and the
parent/grandparent activity guard remains in force until the workflow finishes.
Job order still defines the selection for **Run from this job**. Partial runs
do not release downstream workflows. Install the DEV column/procedures with
`python backend/migrations/apply_workflow_job_execution.py --apply`; without
`--apply` the command is read-only. The opt-in
`backend/tests/live_workflow_job_execution.py --run` uses and removes isolated
DEV validation workflows.

**Workflow → Monitor** reads the real `WORKFLOW`, `JOB`, `DEPENDENCIES`,
and `WORKFLOW_EXECUTION_LOG` tables through a read-only endpoint. It refreshes
automatically and keeps one execution's workflow/job statuses together: active
first, otherwise the oldest queued request, otherwise the latest result.
Each configured job receives a status. Disabled and out-of-scope jobs are
`SKIPPED`; later selected jobs are `PENDING` while the first goes through
`INITIATING`, `QUEUED` and `RUNNING`. Progress includes run-scoped dbt model/test
counts. Older partial executions can still show `NOT SELECTED`. Workflows without execution records show
`NOT RUN`. Click a workflow name for its execution history. Workflow Run executes
all enabled jobs using the workflow's Sequential/Parallel setting; a job Run
offers only that job or from that job onward.
Disabled jobs are omitted from the execution preview and recorded as skipped.
Right-aligned green play buttons and three-dot actions are in the Workflow / Job
column. Live activity indicators identify the workflow and current job.

Manual API execution requires `KUMO_ORCHESTRATION_RUNS_ENABLED=true` in the
running backend; the code default is false. Initial deployment prepares workflow
schedulers suspended; enable individual schedules from the Monitor. DEV now uses a
stream-triggered dispatcher and per-execution finalizers to advance accepted
requests, recover native failures and clean temporary tasks. There is no
periodic maintenance schedule. TST has not been promoted to this dispatch change.
TST retains its existing engine until the new engine has been tested and accepted. See the [parallel engine guide](snowflake/README.md#parallel-orchestration-engine).

The Dashboard uses the same shared workflow/job snapshot as Workflow Monitor,
including active submissions, job progress and enabled schedules. Last Run uses
Stockholm calendar days: blue **Today**, yellow **Yesterday**, otherwise a date.

Manual dbt job runs offer **Full refresh** for `build`, `run` and `seed`. It adds
`--full-refresh` to the selected job's execution only; **Run from this job** keeps
later jobs' saved commands. Job definitions and scheduled runs are unchanged.
Before releasing this UI, run the targeted procedure migration in each intended
environment (it inspects only unless `--apply` is supplied):

```bash
python backend/migrations/apply_manual_dbt_full_refresh.py --environment DEV
python backend/migrations/apply_manual_dbt_full_refresh.py --environment DEV --apply
```

Use `--environment TST` for TST. Finish pending workflow runs first and use the
procedure owner role. The migration updates only admission and command resolution,
preserves usage grants and retains existing five-argument calls via an optional
boolean. The signature change requires replacing the old overload, following
[Snowflake's optional argument rules](https://docs.snowflake.com/en/sql-reference/sql/create-procedure).

DEV now uses the new Workflow Monitor for manual workflow/job runs. The old
execution tasks, procedures, queue and stream are suspended/archived with `X_`
names; old history remains available. DEV's environment-specific retirement
parameter blocks the old controls and recovery worker. TST is unchanged.
See [DEV retirement](snowflake/README.md#dev-retirement-of-the-old-execution-engine)
for the targeted migration. DEV supports downstream dispatch and
[workflow completion emails](snowflake/README.md#workflow-notifications-in-dev-and-tst).

The new Monitor includes Log shortcuts on workflow/job rows and Latest DAG Run
for DBT jobs, quick enable/disable controls, and a schedule editor. Latest DAG Run
uses the job's latest started execution, even when a newer workflow run skipped
that job. DAG Preview belongs to DBT jobs under Workflow configuration and uses
the saved selection with `dbt list` and `WRITEBACK = FALSE` without running models.
Last Run/Duration show the latest
started execution for each row. Schedule hides cron text when disabled; Next Run
comes from the active native workflow scheduler. Workflow disabling pauses its
schedule; enabling it resumes the configured schedule. Accepted executions keep
their saved selection. Job enable/disable applies globally wherever that job is
assigned. DAGs and logs read only the selected new workflow/job execution IDs.

The orchestration Monitor reads one shared Python snapshot. Python refreshes the
Snowflake execution/progress data once per backend process and pushes snapshots
through the existing `/api/events` SSE stream (`orchestration_update`). Initial
load, reconnect, and a disconnected-stream fallback read the shared cache; they
do not query Snowflake per browser. Manual refresh wakes the shared reader.

Manual Run returns HTTP 202 with a native Snowflake query receipt, request ID,
and `INITIATING` status. Snapshot preparation and temporary workflow task startup
continue in the background; SSE reports validation errors and execution states.
The idempotent request ID prevents duplicate executions on retry. In DEV, the
execution snapshot and its launch event commit together. A native stream task
launches it independently of browser connections or Python confirmation workers. Run
controls for the workflow and its jobs are disabled while submitting, queued,
initiating, or running and become available again after completion. No database
DDL changes are required for the HTTP receipt and SSE transport themselves.

Background monitor/dashboard refreshes follow the instance's live SSE client
count: the first connection starts the shared readers and the last disconnect
stops them. Health checks, activity requests and cached snapshot reads do not
start polling. A refresh already in progress can finish; no further refresh
batches begin while the client count is zero. Reconnecting resumes fresh data.
Submission-confirmation workers also wait for a connected client before their
next Snowflake call. Accepted workflows and native scheduled tasks continue
independently in Snowflake. DEV completion/failure recovery follows native
finalizer events; no maintenance clock queries the warehouse. Bootstrap
configuration loads on the first data request, with a retry cooldown on failure.
App import, readiness probes and cache-only reads do not query Snowflake.

Workflow / History reads `WORKFLOW_EXECUTION_LOG`, with workflow/job,
status, period and text filters and cursor pagination. Workflow and job Actions
open the corresponding history scope. Each run expands into its saved job
order; Log and DAG Run open that exact execution and return to the same filters
and expanded rows. DBT jobs link to their model DAG; workflow rows link to the
executed job chain. History loads on demand, coalesces identical requests in a
bounded 15-second cache, and uses the Monitor's existing SSE snapshots for live
statuses. Historical Log/DAG views refresh on demand rather than polling.
