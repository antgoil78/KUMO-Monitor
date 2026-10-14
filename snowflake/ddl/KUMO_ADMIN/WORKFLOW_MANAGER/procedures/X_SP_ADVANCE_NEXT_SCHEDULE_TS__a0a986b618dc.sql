-- Source: KUMO_ADMIN.WORKFLOW_MANAGER.X_SP_ADVANCE_NEXT_SCHEDULE_TS(VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."WORKFLOW_MANAGER";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.WORKFLOW_MANAGER.X_SP_ADVANCE_NEXT_SCHEDULE_TS("P_WORKFLOW_ID" VARCHAR)
RETURNS VARCHAR
LANGUAGE JAVASCRIPT
COMMENT='user-defined procedure'
EXECUTE AS OWNER
AS '
function oneRow(sqlText, binds){
  const rs = snowflake.execute({sqlText, binds: binds || []});
  if (!rs.next()) return null;
  const obj = {};
  for (let i=1; i<=rs.getColumnCount(); i++){
    obj[rs.getColumnName(i)] = rs.getColumnValue(i);
  }
  return obj;
}

function parseBasicField(field, min, max){
  // Supports: *, */n, n
  const f = String(field).trim();
  if (f === "*") return {type:"any"};
  const m = f.match(/^\\*\\/(\\d+)$/);
  if (m) return {type:"step", step: parseInt(m[1],10)};
  const n = parseInt(f,10);
  if (!isNaN(n) && n>=min && n<=max) return {type:"value", value:n};
  throw "Unsupported cron field: " + f;
}

function expandListRangeStep(expr, min, max, allowSevenAsSunday){
  // Supports:
  //  *          -> all
  //  */n        -> step
  //  n          -> single
  //  n-m        -> range
  //  a,b,c      -> list
  //  mixed      -> list of ranges/singles
  // NOTE: for DOW, 7 is treated as 0 (Sunday) if allowSevenAsSunday=true
  const s = String(expr).trim();
  const out = new Set();

  function norm(v){
    let x = v;
    if (allowSevenAsSunday && x === 7) x = 0;
    return x;
  }

  function addVal(v){
    const x = norm(v);
    if (x < min || x > max) throw "Cron value out of range: " + v + " in " + s;
    out.add(x);
  }

  if (s === "*"){
    for (let i=min; i<=max; i++) out.add(i);
    return out;
  }

  const stepM = s.match(/^\\*\\/(\\d+)$/);
  if (stepM){
    const step = parseInt(stepM[1],10);
    if (!step || step <= 0) throw "Invalid step in cron: " + s;
    for (let i=min; i<=max; i++){
      if (i % step === 0) out.add(i);
    }
    return out;
  }

  const parts = s.split(",");
  for (const p0 of parts){
    const p = p0.trim();
    if (!p) continue;

    const rangeM = p.match(/^(\\d+)\\-(\\d+)$/);
    if (rangeM){
      let a = parseInt(rangeM[1],10);
      let b = parseInt(rangeM[2],10);
      a = norm(a); b = norm(b);

      // Allow wrap for DOW ranges like 5-1 (Fri..Mon)
      if (allowSevenAsSunday && a > b){
        for (let i=a; i<=max; i++) addVal(i);
        for (let i=min; i<=b; i++) addVal(i);
      } else {
        if (a > b) throw "Invalid range in cron: " + p;
        for (let i=a; i<=b; i++) addVal(i);
      }
      continue;
    }

    const n = parseInt(p,10);
    if (!isNaN(n)){
      addVal(n);
      continue;
    }

    throw "Unsupported cron token: " + p + " in " + s;
  }

  return out;
}

function nextFromCron(cron, tz, baseDate){
  // cron: "m h dom mon dow"
  const parts = String(cron).trim().split(/\\s+/);
  if (parts.length !== 5) throw "Invalid cron (need 5 fields): " + cron;

  const mm = parseBasicField(parts[0], 0, 59);
  const hh = parseBasicField(parts[1], 0, 23);

  // DOM/MON must be ''*''
  if (String(parts[2]).trim() !== "*" || String(parts[3]).trim() !== "*"){
    throw "Only dom/mon=''*'' supported currently: " + cron;
  }

  // DOW supports *, lists, ranges, */n, single
  const dowSet = expandListRangeStep(parts[4], 0, 6, /*allowSevenAsSunday*/ true);

  // NOTE: We evaluate in UTC. Your tasks are using UTC schedules.
  // If later you need tz-aware cron evaluation, we can convert base into tz and match there.
  const maxMinutes = 60 * 24 * 21; // 21 days horizon
  let d = new Date(baseDate.getTime());
  d.setSeconds(0,0);
  // strictly after base
  d = new Date(d.getTime() + 60000);

  function matches(date){
    const minute = date.getUTCMinutes();
    const hour   = date.getUTCHours();
    const dow    = date.getUTCDay(); // 0=Sun..6=Sat

    const okM =
      mm.type==="any" ? true :
      mm.type==="value" ? (minute === mm.value) :
      (minute % mm.step === 0);

    const okH =
      hh.type==="any" ? true :
      hh.type==="value" ? (hour === hh.value) :
      (hour % hh.step === 0);

    const okD = dowSet.has(dow);

    return okM && okH && okD;
  }

  for (let i=0; i<maxMinutes; i++){
    if (matches(d)) return d;
    d = new Date(d.getTime() + 60000);
  }
  throw "Could not find next schedule within horizon for cron=" + cron;
}

const row = oneRow(`
  SELECT
    WORKFLOW_ID,
    SCHEDULE_CRON AS CRON,
    COALESCE(SCHEDULE_TIMEZONE, ''UTC'') AS TZ,
    NEXT_SCHEDULE_TS
  FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_TASKS
  WHERE WORKFLOW_ID = ?
`, [P_WORKFLOW_ID]);

if (!row) return "NO_ROW";

const cron = String(row.CRON || "").trim();
const tz   = String(row.TZ || "UTC").trim();
if (!cron) return "NO_CRON";

let base = row.NEXT_SCHEDULE_TS ? new Date(row.NEXT_SCHEDULE_TS) : new Date();
const nextUtc = nextFromCron(cron, tz, base);

// store as TIMESTAMP_NTZ (UTC string)
snowflake.execute({
  sqlText: `
    UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_TASKS
       SET NEXT_SCHEDULE_TS = TO_TIMESTAMP_NTZ(?),
           UPDATED_AT = CURRENT_TIMESTAMP()
     WHERE WORKFLOW_ID = ?
  `,
  binds: [ nextUtc.toISOString().replace("T"," ").replace("Z",""), P_WORKFLOW_ID ]
});

return "OK";
';
