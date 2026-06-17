import json
from datetime import datetime, date, timezone, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo


def sql_escape(value):
    return "" if value is None else str(value).replace("'", "''")


def row_get(row, *keys, default=None):
    for key in keys:
        if key in row:
            return row[key]
        up = key.upper()
        if up in row:
            return row[up]
        low = key.lower()
        if low in row:
            return row[low]
    return default


def to_jsonable(value):
    if isinstance(value, (datetime, date)):
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, Decimal):
        if value == value.to_integral():
            return int(value)
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def normalize_row(row):
    return {str(k).upper(): to_jsonable(v) for k, v in dict(row).items()}


def normalize_rows(rows):
    return [normalize_row(r) for r in rows]


def parse_variant_array(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    s = str(value).strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if str(x).strip()]
    except Exception:
        return []
    return []


def format_task_name(workflow_id, db, schema):
    short = str(workflow_id).replace("-", "")[:8].upper()
    return f"{db}.{schema}.TASK_WF_{short}"


def _parse_cron_field(field, min_v, max_v):
    field = (field or "").strip()
    if not field:
        return set()
    out = set()
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            out.update(range(min_v, max_v + 1))
            continue
        try:
            if "/" in part:
                left, step_s = part.split("/", 1)
                step = max(1, int(step_s))
                if left in ("", "*"):
                    a, b = min_v, max_v
                elif "-" in left:
                    a_s, b_s = left.split("-", 1)
                    a, b = int(a_s), int(b_s)
                else:
                    a, b = int(left), max_v
                out.update(range(max(min_v, a), min(max_v, b) + 1, step))
            elif "-" in part:
                a_s, b_s = part.split("-", 1)
                out.update(range(max(min_v, int(a_s)), min(max_v, int(b_s)) + 1))
            else:
                value = int(part)
                if min_v <= value <= max_v:
                    out.add(value)
        except Exception:
            continue
    return out


def next_run(cron_expr, tz_name):
    cron_expr = (cron_expr or "").strip()
    tz_name = (tz_name or "UTC").strip() or "UTC"
    if not cron_expr or cron_expr == "-":
        return None
    parts = cron_expr.split()
    if len(parts) != 5:
        return None
    minute_f, hour_f, dom_f, mon_f, dow_f = parts
    mins = _parse_cron_field(minute_f, 0, 59)
    hrs = _parse_cron_field(hour_f, 0, 23)
    doms = _parse_cron_field(dom_f, 1, 31)
    mons = _parse_cron_field(mon_f, 1, 12)
    dows = _parse_cron_field(dow_f, 0, 6)
    if not mins or not hrs or not doms or not mons or not dows:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    current = datetime.now(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(14 * 24 * 60):
        cron_dow = (current.weekday() + 1) % 7
        if (
            current.minute in mins
            and current.hour in hrs
            and current.day in doms
            and current.month in mons
            and cron_dow in dows
        ):
            return current.astimezone(ZoneInfo("Europe/Stockholm")).isoformat()
        current += timedelta(minutes=1)
    return None
