// Visualization fixtures shaped like WORKFLOW, JOB and WORKFLOW_JOB.
// Execution state is illustrative and kept separate from catalog definitions.
export const sampleSnapshot = '2026-10-23T08:15:00+02:00'

export const workflows = [
  { WORKFLOW_ID: 'wf-customer', WORKFLOW_NAME: 'Customer daily load', WORKFLOW_GROUP: 'Customer', SCHEDULE: '0 8 * * *', SCHEDULE_TIMEZONE: 'Europe/Stockholm', IS_SCHEDULE_ENABLED: true, IS_ENABLED: true },
  { WORKFLOW_ID: 'wf-finance', WORKFLOW_NAME: 'Finance reconciliation', WORKFLOW_GROUP: 'Finance', SCHEDULE: '0 7 * * *', SCHEDULE_TIMEZONE: 'Europe/Stockholm', IS_SCHEDULE_ENABLED: true, IS_ENABLED: true },
  { WORKFLOW_ID: 'wf-sales', WORKFLOW_NAME: 'Sales daily load', WORKFLOW_GROUP: 'Sales', SCHEDULE: '0 6 * * *', SCHEDULE_TIMEZONE: 'Europe/Stockholm', IS_SCHEDULE_ENABLED: true, IS_ENABLED: true },
  { WORKFLOW_ID: 'wf-reporting', WORKFLOW_NAME: 'Reporting refresh', WORKFLOW_GROUP: 'Analytics', SCHEDULE: null, ALLOW_EXTERNAL_TRIGGER: true, IS_ENABLED: true },
  { WORKFLOW_ID: 'wf-inventory', WORKFLOW_NAME: 'Inventory load', WORKFLOW_GROUP: 'Supply chain', SCHEDULE: null, ALLOW_MANUAL_TRIGGER: true, IS_ENABLED: false }
]

export const jobs = [
  { JOB_ID: 'job-customer-raw', JOB_NAME: 'Load customer source', JOB_TYPE: 'SQL', JOB_LAYER: 'RAW' },
  { JOB_ID: 'job-customer-sdl', JOB_NAME: 'Stage customer records', JOB_TYPE: 'DBT', JOB_LAYER: 'SDL' },
  { JOB_ID: 'job-customer-edv', JOB_NAME: 'Build customer vault', JOB_TYPE: 'DBT', JOB_LAYER: 'EDV' },
  { JOB_ID: 'job-customer-bdl', JOB_NAME: 'Publish customer dimensions', JOB_TYPE: 'DBT', JOB_LAYER: 'BDL' },
  { JOB_ID: 'job-finance-raw', JOB_NAME: 'Load ledger entries', JOB_TYPE: 'SQL', JOB_LAYER: 'RAW' },
  { JOB_ID: 'job-finance-edv', JOB_NAME: 'Reconcile ledger balances', JOB_TYPE: 'DBT', JOB_LAYER: 'EDV' },
  { JOB_ID: 'job-finance-bdl', JOB_NAME: 'Publish finance mart', JOB_TYPE: 'DBT', JOB_LAYER: 'BDL' },
  { JOB_ID: 'job-sales-raw', JOB_NAME: 'Load sales transactions', JOB_TYPE: 'SQL', JOB_LAYER: 'RAW' },
  { JOB_ID: 'job-sales-bdl', JOB_NAME: 'Build sales mart', JOB_TYPE: 'DBT', JOB_LAYER: 'BDL' },
  { JOB_ID: 'job-reporting', JOB_NAME: 'Refresh reporting aggregates', JOB_TYPE: 'SQL', JOB_LAYER: 'BDL' },
  { JOB_ID: 'job-checks', JOB_NAME: 'Run quality checks', JOB_TYPE: 'DBT', JOB_LAYER: 'GENERAL' }
]

const memberships = {
  'wf-customer': ['job-customer-raw', 'job-customer-sdl', 'job-customer-edv', 'job-customer-bdl'],
  'wf-finance': ['job-finance-raw', 'job-finance-edv', 'job-finance-bdl'],
  'wf-sales': ['job-sales-raw', 'job-sales-bdl', 'job-checks'],
  'wf-reporting': ['job-reporting', 'job-checks'],
  'wf-inventory': []
}

export const assignments = Object.entries(memberships).flatMap(([workflowId, jobIds]) => jobIds.map((jobId, index) => ({
  WORKFLOW_JOB_ID: `${workflowId}-${jobId}`, WORKFLOW_ID: workflowId, JOB_ID: jobId,
  JOB_SEQUENCE: index + 1, PARENT_JOB_ID: jobIds[index - 1] || null,
  DEPENDENCY_TYPE: 'SEQUENCE', MIN_STATUS_LEVEL: 'SUCCESS', IS_ENABLED: true
})))

function run(status, start, end, message = '') {
  return { status, start: start ? `2026-10-23T${start}+02:00` : null, end: end ? `2026-10-23T${end}+02:00` : null, message }
}

export const workflowRuns = {
  'wf-customer': run('RUNNING', '08:00:00'),
  'wf-finance': run('FAILED', '07:00:00', '07:06:12', 'Ledger balance validation failed'),
  'wf-sales': run('SUCCESS', '06:00:00', '06:12:46'),
  'wf-reporting': run('QUEUED'),
  'wf-inventory': run('NOT RUN')
}

// Runs belong to an assignment, so a reused job can have different run states.
export const jobRuns = {
  'wf-customer-job-customer-raw': run('SUCCESS', '08:00:00', '08:02:18'),
  'wf-customer-job-customer-sdl': run('SUCCESS', '08:02:18', '08:05:42'),
  'wf-customer-job-customer-edv': run('RUNNING', '08:05:42'),
  'wf-customer-job-customer-bdl': run('PENDING'),
  'wf-finance-job-finance-raw': run('SUCCESS', '07:00:00', '07:03:10'),
  'wf-finance-job-finance-edv': run('FAILED', '07:03:10', '07:06:12', 'Ledger balance validation failed'),
  'wf-finance-job-finance-bdl': run('SKIPPED', null, null, 'Upstream job failed'),
  'wf-sales-job-sales-raw': run('SUCCESS', '06:00:00', '06:04:22'),
  'wf-sales-job-sales-bdl': run('SUCCESS', '06:04:22', '06:11:08'),
  'wf-sales-job-checks': run('SUCCESS', '06:11:08', '06:12:46'),
  'wf-reporting-job-reporting': run('QUEUED'),
  'wf-reporting-job-checks': run('PENDING')
}
