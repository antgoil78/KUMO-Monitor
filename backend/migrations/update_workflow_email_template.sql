CREATE OR REPLACE PROCEDURE KUMO_ADMIN.WORKFLOW_MANAGER.SP_SEND_WORKFLOW_EMAIL(
    P_INTEGRATION_NAME VARCHAR,
    P_TO VARCHAR,
    P_ENV VARCHAR,
    P_SEVERITY VARCHAR,
    P_RUN_ID VARCHAR,
    P_SUBJECT_AREA VARCHAR,
    P_SOURCE_ID VARCHAR,
    P_PKG_GROUP_NAME VARCHAR,
    P_PHASE VARCHAR,
    P_EVENT_TYPE VARCHAR,
    P_EVENT_DTTM TIMESTAMP_NTZ,
    P_ROWS_AFFECTED NUMBER,
    P_DETAILS_JSON VARIANT
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    V_SUBJECT STRING;
    V_HTML STRING;
    V_WORKFLOW_NAME STRING;
    V_DETAILS_PRETTY STRING;
BEGIN
    V_WORKFLOW_NAME := COALESCE(P_PKG_GROUP_NAME, P_SUBJECT_AREA, '(unknown workflow)');
    V_SUBJECT := '[KUMO Monitor][' || P_ENV || '][' || P_SEVERITY || '] '
                 || V_WORKFLOW_NAME || ' - ' || COALESCE(P_EVENT_TYPE, 'EVENT');

    SELECT COALESCE(
        '{\n' || LISTAGG(
            '  "' || REPLACE(f.KEY, '"', '\\"') || '": ' || TO_JSON(f.VALUE),
            ',\n'
        ) WITHIN GROUP (ORDER BY f.KEY) || '\n}',
        TO_JSON(:P_DETAILS_JSON)
    )
    INTO :V_DETAILS_PRETTY
    FROM TABLE(FLATTEN(INPUT => :P_DETAILS_JSON)) f;

    V_DETAILS_PRETTY := REPLACE(
        REPLACE(
            REPLACE(COALESCE(V_DETAILS_PRETTY, '{}'), '&', '&amp;'),
            '<', '&lt;'
        ),
        '>', '&gt;'
    );

    V_HTML := '
<!doctype html>
<html>
<head><meta charset="utf-8" /></head>
<body style="margin:0; padding:0; font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif; background:#050e24; color:#e8f0ff;">
  <div style="max-width:820px; margin:0 auto; padding:28px 20px;">
    <div style="background:linear-gradient(135deg,#0b1c42,#102c5c); border:1px solid #1f4778; padding:20px 22px; border-radius:14px 14px 0 0;">
      <div style="color:#01e8ff; font-size:12px; font-weight:800; letter-spacing:1.5px; text-transform:uppercase;">' || P_ENV || ' · ' || P_SEVERITY || '</div>
      <div style="font-size:22px; font-weight:800; margin-top:6px; color:#ffffff;">KUMO Monitor</div>
      <div style="font-size:14px; color:#9db3d5; margin-top:5px;">' || COALESCE(P_EVENT_TYPE, 'Workflow notification') || '</div>
    </div>

    <div style="background:#091936; border:1px solid #1f4778; border-top:0; border-radius:0 0 14px 14px; padding:20px 22px;">
      <table cellpadding="0" cellspacing="0" style="width:100%; border-collapse:collapse; font-size:13px; color:#e8f0ff;">
        <tr><td style="padding:10px; color:#8299bd; width:150px; border-bottom:1px solid #18345e;">Severity</td><td style="padding:10px; border-bottom:1px solid #18345e; font-weight:800; color:#ffffff;">' || P_SEVERITY || '</td></tr>
        <tr><td style="padding:10px; color:#8299bd; border-bottom:1px solid #18345e;">Event time</td><td style="padding:10px; border-bottom:1px solid #18345e;">' || TO_VARCHAR(P_EVENT_DTTM, 'YYYY-MM-DD HH24:MI:SS') || '</td></tr>
        <tr><td style="padding:10px; color:#8299bd; border-bottom:1px solid #18345e;">Run ID</td><td style="padding:10px; border-bottom:1px solid #18345e; font-family:Consolas,monospace; color:#72e8f5;">' || P_RUN_ID || '</td></tr>
        <tr><td style="padding:10px; color:#8299bd; border-bottom:1px solid #18345e;">Workflow name</td><td style="padding:10px; border-bottom:1px solid #18345e; font-weight:700; color:#ffffff;">' || V_WORKFLOW_NAME || '</td></tr>
      </table>

      <div style="margin-top:20px; color:#9db3d5; font-size:12px; font-weight:800; letter-spacing:.8px; text-transform:uppercase;">Details (JSON)</div>
      <pre style="margin:9px 0 0; padding:16px; background:#030a18; color:#cce8ef; border:1px solid #18345e; border-radius:10px; overflow:auto; white-space:pre-wrap; word-break:break-word; font-family:Consolas,Monaco,monospace; font-size:12px; line-height:1.55;">' || V_DETAILS_PRETTY || '</pre>

      <div style="margin-top:16px; color:#7188ab; font-size:11px;">Generated automatically by KUMO Monitor.</div>
    </div>
  </div>
</body>
</html>';

    CALL SYSTEM$SEND_EMAIL(
        :P_INTEGRATION_NAME,
        :P_TO,
        :V_SUBJECT,
        :V_HTML,
        'text/html'
    );

    RETURN 'EMAIL_SENT';
END;
$$;
