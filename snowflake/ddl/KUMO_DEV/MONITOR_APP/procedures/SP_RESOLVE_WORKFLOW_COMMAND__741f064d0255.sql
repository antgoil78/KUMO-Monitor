-- Source: KUMO_DEV.MONITOR_APP.SP_RESOLVE_WORKFLOW_COMMAND(VARIANT, VARCHAR)
-- Refresh: python snowflake/export_ddl.py --object PROCEDURE 'KUMO_DEV.MONITOR_APP.SP_RESOLVE_WORKFLOW_COMMAND(VARIANT, VARCHAR)'
USE DATABASE "KUMO_DEV";
USE SCHEMA "KUMO_DEV"."MONITOR_APP";

CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_RESOLVE_WORKFLOW_COMMAND("P_DEFINITION" VARIANT, "P_RUN_ID" VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
EXECUTE AS OWNER
AS '
// Shared by Snowflake execution and the app''s read-only command preview.
function fail(message) { throw new Error(message); }
function sqlEscape(value) { return String(value).replace(/''/g, "''''"); }
function identifier(value) { return /^[A-Za-z_][A-Za-z0-9_$]*$/.test(value); }
function shellQuote(value) {
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(value) ? value : "''" + value.replace(/''/g, "''\\"''\\"''") + "''";
}
function splitArgs(command) {
  var result = [], token = '''', quote = null, escaped = false, started = false;
  for (var i = 0; i < command.length; i++) {
    var c = command[i];
    if (escaped) { token += c; escaped = false; started = true; }
    else if (c === ''\\\\'' && quote !== "''") { escaped = true; started = true; }
    else if (quote) { if (c === quote) quote = null; else token += c; }
    else if (c === "''" || c === ''"'') { quote = c; started = true; }
    else if (/\\s/.test(c)) { if (started) result.push(token); token = ''''; started = false; }
    else { token += c; started = true; }
  }
  if (quote || escaped) fail(''Invalid dbt command: unfinished quote or escape'');
  if (started) result.push(token);
  return result;
}

try {
  var definition = P_DEFINITION || {};
  var sampleRunId = ''00000000-0000-0000-0000-000000000000'';
  var runId = P_RUN_ID === null || P_RUN_ID === undefined ? sampleRunId : String(P_RUN_ID);
  if (!/^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/.test(runId)) fail(''Invalid workflow run ID'');
  var contextRows = snowflake.execute({sqlText: "CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()"});
  contextRows.next();
  var environment = contextRows.getColumnValue(1);
  if (typeof environment === ''string'') environment = JSON.parse(environment);
  var parameters = environment._parameters, used = Object.create(null);
  function parameter(key) {
    if (key === ''run_id'') return runId;
    var entry = parameters[key];
    if (!entry || !entry.active) fail(''Missing or inactive environment parameter: ['' + key + '']'');
    if (entry.secret) fail(''Secret parameters cannot be used in workflow commands: ['' + key + '']'');
    if (entry.value === null || String(entry.value).trim() === '''') fail(''Empty environment parameter: ['' + key + '']'');
    var value = String(entry.value);
    if ([''DATABASE'', ''MONITOR_APP_SCHEMA'', ''META_SCHEMA'', ''APP_SCHEMA'', ''RAW_SCHEMA'', ''DBT_SCHEMA'', ''DBT_PROJECT'', ''DBT_TARGET''].indexOf(key) >= 0 &&
        (!identifier(value) || entry.type !== ''STRING'')) fail(''Invalid identifier parameter: ['' + key + '']'');
    used[key] = value;
    return value;
  }
  var database = parameter(''DATABASE'');

  function render(text, mode) {
    text = String(text || '''');
    if (/#\\[[A-Za-z_][A-Za-z0-9_]*\\]#/.test(text)) fail(''Use [VARIABLE] without # in workflow commands'');
    var output = '''', quote = null, comment = null;
    for (var i = 0; i < text.length;) {
      var c = text[i], pair = text.substr(i, 2);
      if (mode === ''sql'' && comment) {
        output += c; i++;
        if (comment === ''line'' && (c === ''\\n'' || c === ''\\r'')) comment = null;
        else if (comment === ''block'' && pair === ''*/'') { output += ''/''; i++; comment = null; }
        continue;
      }
      if (mode === ''sql'' && !quote && (pair === ''--'' || pair === ''/*'')) {
        comment = pair === ''--'' ? ''line'' : ''block''; output += pair; i += 2; continue;
      }
      if (c === ''['') {
        var token = /^\\[([A-Za-z_][A-Za-z0-9_]*)\\]/.exec(text.substr(i));
        if (token) {
          var value = parameter(token[1]);
          if (mode === ''sql'') {
            if (quote === "''") value = sqlEscape(value);
            else if (quote === ''"'') value = value.replace(/"/g, ''""'');
            else if (!identifier(value) && !/^-?\\d+(?:\\.\\d+)?$/.test(value))
              fail(''Put ['' + token[1] + ''] inside single quotes when used as a string value'');
          }
          output += value; i += token[0].length; continue;
        }
      }
      if (mode === ''sql'' && (c === "''" || c === ''"'')) {
        if (quote === c && text[i + 1] === c) { output += c + c; i += 2; continue; }
        if (quote === c) quote = null; else if (!quote) quote = c;
      }
      output += c; i++;
    }
    return output;
  }
  var workflowType = String(definition.workflowType || definition.jobType || ''SQL'').toUpperCase();
  var result = {ok: true, workflowType: workflowType, runId: runId, runIdIsExample: runId === sampleRunId};
  if (workflowType === ''SQL'') {
    result.sqlCommand = render(definition.sqlCommand, ''sql'').trim().replace(/;+\\s*$/, '''');
    if (!result.sqlCommand) fail(''SQL command is required'');
    result.execSql = result.sqlCommand;
  } else if (workflowType === ''DBT'') {
    result.dbtProjectFqn = render(definition.dbtProjectFqn || ''[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]'', ''identifier'').trim();
    result.dbtTarget = render(definition.dbtTarget || ''[DBT_TARGET]'', ''identifier'').trim();
    if (!/^[A-Za-z_][A-Za-z0-9_$]*(?:\\.[A-Za-z_][A-Za-z0-9_$]*){2}$/.test(result.dbtProjectFqn)) fail(''Invalid resolved dbt project name'');
    if (result.dbtProjectFqn.split(''.'')[0].toUpperCase() !== database.toUpperCase()) fail(''The dbt project must belong to [DATABASE]'');
    if (!identifier(result.dbtTarget)) fail(''Invalid resolved dbt target'');
    var tokens = splitArgs(String(definition.dbtCommand || '''').trim());
    if (tokens.length && tokens[0].toLowerCase() === ''dbt'') tokens.shift();
    if (!tokens.length || !/^(build|run|test|list|compile|parse|seed|snapshot|run-operation|retry|debug|deps|clean)$/.test(tokens[0]))
      fail(''A supported dbt command is required'');
    tokens = tokens.map(function (token) { return render(token, ''cli''); });
    var args = [], vars = {}, explicitTarget = null, seenVars = false;
    for (var i = 0; i < tokens.length; i++) {
      var token = tokens[i], targetMatch = /^(?:--target|-t)=(.*)$/.exec(token), varsMatch = /^--vars=(.*)$/.exec(token);
      if (token === ''--target'' || token === ''-t'' || targetMatch) {
        if (explicitTarget !== null) fail(''Specify the dbt target only once'');
        explicitTarget = targetMatch ? targetMatch[1] : tokens[++i];
        if (explicitTarget !== result.dbtTarget) fail(''Command target conflicts with the configured dbt target; use [DBT_TARGET]'');
      } else if (token === ''--vars'' || varsMatch) {
        if (seenVars) fail(''Specify dbt --vars only once'');
        seenVars = true;
        try { vars = JSON.parse(varsMatch ? varsMatch[1] : tokens[++i]); }
        catch (e) { fail(''Use a JSON object for dbt --vars''); }
        if (!vars || typeof vars !== ''object'' || Array.isArray(vars)) fail(''Use a JSON object for dbt --vars'');
      } else args.push(token);
    }
    vars.runId = runId;
    result.dbtVars = vars;
    args.push(''--vars'', JSON.stringify(vars), ''--target'', result.dbtTarget);
    result.dbtCommand = args.map(shellQuote).join('' '');
    result.execSql = ''EXECUTE DBT PROJECT '' + result.dbtProjectFqn + " ARGS = ''" + sqlEscape(result.dbtCommand) + "''";
  } else fail(''Command preview supports SQL and dbt definitions'');
  result.parametersUsed = used;
  return result;
} catch (error) {
  return {ok: false, error: String(error.message || error)};
}
';
