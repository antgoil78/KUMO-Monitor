"""Append dependency decisions inside the transaction that makes them durable."""
from datetime import datetime
import json
import uuid


def _json_default(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError('Unsupported decision fact type: '+type(value).__name__)


def record(conn, component, decision, *, graph_id=None, graph_revision=None,
           cascade_id=None, node_key=None, run_id=None, schedule_id=None,
           occurrence_id=None, evaluation_id=None, request_id=None, actor=None,
           **facts):
    """Facts contain identities, states and rules, never SQL or connection data."""
    from psycopg.types.json import Jsonb
    conn.execute('''INSERT INTO kumo_task_engine.dependency_decisions
        (component,decision,graph_id,graph_revision,cascade_id,node_key,run_id,
         schedule_id,occurrence_id,evaluation_id,request_id,actor,facts)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (component,decision,graph_id,graph_revision,cascade_id,node_key,run_id,
         schedule_id,occurrence_id,evaluation_id,request_id,
         str(actor)[:256] if actor is not None else None,
         Jsonb(facts, dumps=lambda value: json.dumps(value, default=_json_default))))
