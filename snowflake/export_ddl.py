"""Save changed app objects, or explicitly refresh both app schemas."""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import snowflake.connector
from snowflake.connector import DictCursor


ROOT = Path(__file__).resolve().parent
SHOW_TYPES = {
    "TABLE": "TABLES", "SEQUENCE": "SEQUENCES", "FUNCTION": "USER FUNCTIONS",
    "VIEW": "VIEWS", "PROCEDURE": "PROCEDURES", "TASK": "TASKS",
    "STREAM": "STREAMS", "FILE_FORMAT": "FILE FORMATS",
}
IDENTIFIER = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
REFERENCE = re.compile(r'\bREFERENCES\s+(' + IDENTIFIER + r'(?:\s*\.\s*' + IDENTIFIER + r'){0,2})', re.I)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def qualified(name):
    return '.'.join(quote(part) for part in name.split('.'))


def excluded_task(fqn, scope):
    if fqn in scope['keep_tasks']:
        return False
    return any(re.search(pattern, fqn.rsplit('.', 1)[-1], re.I)
               for pattern in scope['exclude_task_patterns'])


def signature(arguments):
    # SHOW output includes DEFAULT/OUT markers and a return type, neither of
    # which belongs in the GET_DDL overload signature. Keep nested type sizes.
    start = arguments.index('(')
    depth = 0
    for end in range(start, len(arguments)):
        depth += (arguments[end] == '(') - (arguments[end] == ')')
        if depth == 0:
            value = arguments[start:end + 1]
            return re.sub(r'\b(?:DEFAULT|OUT|INOUT|IN)\s+', '', value, flags=re.I)
    raise ValueError('Unbalanced callable signature: ' + arguments)


def object_path(kind, schema, name, args=''):
    suffix = ''
    if args:
        # Names remain readable; the signature hash distinguishes overloads.
        suffix = '__' + hashlib.sha256(args.encode()).hexdigest()[:12]
    filename = re.sub(r'[^A-Za-z0-9_$.-]', '_', name) + suffix + '.sql'
    directory = SHOW_TYPES[kind].lower().replace('user ', '').replace(' ', '_')
    return Path('ddl') / Path(*schema.split('.')) / directory / filename


def object_key(kind, name, args=''):
    return kind, name.upper(), re.sub(r'\s+', '', args).upper()


def requested_objects(items, scope):
    requested = set()
    for kind, identifier in items or []:
        kind = kind.upper()
        match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*)(\(.*\))?', identifier)
        if kind not in SHOW_TYPES or not match:
            raise ValueError('Use an object type and database.schema.object name, with a signature for procedures/functions')
        name, args = match.group(1).upper(), match.group(2) or ''
        if name.rsplit('.', 1)[0] not in scope['schemas']:
            raise ValueError('Object is outside the maintained app schemas: ' + name)
        if bool(args) != (kind in ('FUNCTION', 'PROCEDURE')):
            raise ValueError('Only procedures/functions require an argument signature: ' + name)
        if kind == 'TASK' and excluded_task(name, scope):
            raise ValueError('Generated workflow tasks are not saved: ' + name)
        requested.add(object_key(kind, name, args))
    return requested


def table_order(objects, definitions):
    tables = {obj['name']: obj for obj in objects if obj['type'] == 'TABLE'}
    dependencies = {}
    for name, obj in tables.items():
        parents = set()
        for match in REFERENCE.finditer(definitions[obj['file']]):
            parts = re.findall(IDENTIFIER, match.group(1))
            parts = [part[1:-1].replace('""', '"') if part.startswith('"') else part.upper() for part in parts]
            if len(parts) == 1:
                parts = name.split('.')[:2] + parts
            elif len(parts) == 2:
                parts = name.split('.')[:1] + parts
            parent = '.'.join(parts)
            if parent in tables and parent != name:
                parents.add(parent)
        dependencies[name] = parents
    ordered = []
    while dependencies:
        ready = sorted(name for name, parents in dependencies.items() if not parents)
        if not ready:
            raise RuntimeError('Circular table foreign keys require a separate constraint migration: ' + ', '.join(sorted(dependencies)))
        for name in ready:
            ordered.append(tables[name]['file'])
            del dependencies[name]
        for parents in dependencies.values():
            parents.difference_update(ready)
    return ordered


def build_restore(destination):
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    parts = ['-- Restore into a new account after provisioning prerequisites.\n'
             '-- Definitions contain CREATE OR REPLACE. Table contents are not included.\n'
             '-- Tasks are created suspended; resume explicitly after configuration.\n']
    for filename in manifest['restore_order']:
        parts.append('-- File: ' + filename + '\n' + (ROOT / filename).read_text())
    destination = Path(destination).expanduser().resolve()
    if destination.is_relative_to(ROOT):
        raise ValueError('Write the restore bundle outside snowflake/ to avoid duplicated tracked SQL')
    destination.write_text('\n'.join(parts))
    print('Wrote restore bundle:', destination)


def export(role=None, items=None, refresh=False):
    scope = json.loads((ROOT / 'export_scope.json').read_text())
    requested = requested_objects(items, scope)
    if not requested and not refresh:
        raise ValueError('Choose --object for changed objects or --refresh for an explicitly requested schema refresh')
    if requested and refresh:
        raise ValueError('Choose either --object or --refresh')
    previous = json.loads((ROOT / 'manifest.json').read_text()) if (ROOT / 'manifest.json').exists() else {}
    load_dotenv(ROOT.parent / '.env')
    kwargs = {
        'account': os.environ['SNOWFLAKE_ACCOUNT'],
        'user': os.environ['SNOWFLAKE_USER'],
        'password': os.environ['SNOWFLAKE_PASSWORD'],
        'login_timeout': 20, 'network_timeout': 90,
        'session_parameters': {'QUERY_TAG': 'KUMO_MONITOR_DDL_EXPORT', 'STATEMENT_TIMEOUT_IN_SECONDS': 300},
    }
    if role or os.getenv('SNOWFLAKE_ROLE'):
        kwargs['role'] = role or os.environ['SNOWFLAKE_ROLE']
    if os.getenv('SNOWFLAKE_WAREHOUSE'):
        kwargs['warehouse'] = os.environ['SNOWFLAKE_WAREHOUSE']
    if os.getenv('SNOWFLAKE_PASSCODE'):
        kwargs['passcode'] = os.environ['SNOWFLAKE_PASSCODE']
    kwargs['passcode_in_password'] = os.getenv('SNOWFLAKE_PASSCODE_IN_PASSWORD', 'false').lower() == 'true'

    objects, definitions, excluded = [], {}, []
    with snowflake.connector.connect(**kwargs) as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute('SELECT CURRENT_ACCOUNT_NAME() AS ACCOUNT_NAME, CURRENT_ROLE() AS ROLE_NAME')
            source = cur.fetchone()
            databases = sorted({schema.split('.')[0] for schema in scope['schemas']})
            bootstrap = ['-- Create namespaces in the destination account.\n']
            for database in databases:
                bootstrap.append('CREATE DATABASE IF NOT EXISTS ' + quote(database) + ';')
            for schema in scope['schemas']:
                bootstrap.append('CREATE SCHEMA IF NOT EXISTS ' + qualified(schema) + ';')
                if requested and not any(key[1].rsplit('.', 1)[0] == schema for key in requested):
                    continue
                for kind in scope['object_types']:
                    if requested and not any(key[0] == kind and key[1].rsplit('.', 1)[0] == schema for key in requested):
                        continue
                    cur.execute('SHOW ' + SHOW_TYPES[kind] + ' IN SCHEMA ' + qualified(schema))
                    rows = cur.fetchall()
                    if len(rows) >= 10000:
                        raise RuntimeError('SHOW result may be truncated: ' + schema + ' ' + kind)
                    for row in sorted(rows, key=lambda item: (item['name'], item.get('arguments', ''))):
                        # SHOW PROCEDURES includes Snowflake built-ins with no schema.
                        if kind in ('FUNCTION', 'PROCEDURE') and row.get('is_builtin') == 'Y':
                            continue
                        if kind in ('FUNCTION', 'PROCEDURE') and not row.get('schema_name'):
                            continue
                        name = schema + '.' + row['name']
                        if kind == 'TASK' and excluded_task(name, scope):
                            excluded.append(name)
                            continue
                        args = signature(row['arguments']) if kind in ('FUNCTION', 'PROCEDURE') else ''
                        if requested and object_key(kind, name, args) not in requested:
                            continue
                        identifier = qualified(schema) + '.' + quote(row['name']) + args
                        cur.execute('SELECT GET_DDL(%s, %s, TRUE) AS DDL', (kind, identifier))
                        ddl = cur.fetchone()['DDL']
                        if not ddl or '#UNKNOWN_' in ddl:
                            raise RuntimeError('Incomplete or masked DDL: ' + name)
                        if kwargs['password'] and kwargs['password'] in ddl:
                            raise RuntimeError('A connection secret appears in DDL: ' + name)
                        path = object_path(kind, schema, row['name'], args).as_posix()
                        if path in definitions:
                            raise RuntimeError('Duplicate export path: ' + path)
                        header = ('-- Source: ' + name + args + '\n'
                                  '-- Refresh: python snowflake/export_ddl.py --object ' + kind + " '" + name + args + "'\n"
                                  'USE DATABASE ' + quote(schema.split('.')[0]) + ';\n'
                                  'USE SCHEMA ' + qualified(schema) + ';\n\n')
                        definitions[path] = header + ddl.rstrip().rstrip(';') + ';\n'
                        obj = {'type': kind, 'name': name, 'file': path}
                        if args:
                            obj['signature'] = args
                        if kind == 'TASK':
                            obj['source_state'] = row['state']
                            obj['predecessors'] = json.loads(row.get('predecessors') or '[]')
                        objects.append(obj)
                print('Read definitions:', schema, flush=True)
    if requested:
        found = {object_key(obj['type'], obj['name'], obj.get('signature', '')) for obj in objects}
        if found != requested:
            raise RuntimeError('Requested objects were not found or visible: ' + repr(sorted(requested - found)))
        # Preserve every untouched saved object, without querying it in Snowflake.
        for obj in previous.get('objects', []):
            key = object_key(obj['type'], obj['name'], obj.get('signature', ''))
            if key not in requested and obj['name'].rsplit('.', 1)[0] in scope['schemas']:
                objects.append(obj)
                definitions[obj['file']] = (ROOT / obj['file']).read_text()
        objects.sort(key=lambda obj: (scope['schemas'].index(obj['name'].rsplit('.', 1)[0]),
                                     scope['object_types'].index(obj['type']), obj['name'], obj.get('signature', '')))
    definitions['00_databases_schemas.sql'] = '\n'.join(bootstrap) + '\n'
    ordered = ['00_databases_schemas.sql']
    ordered.extend(obj['file'] for obj in objects if obj['type'] == 'SEQUENCE')
    ordered.extend(obj['file'] for obj in objects if obj['type'] == 'FILE_FORMAT')
    ordered.extend(table_order(objects, definitions))
    # The inventory currently has one independent helper function and one view.
    # Any future function/view/task dependency graph must be reflected here.
    for kind in ('FUNCTION', 'VIEW', 'STREAM', 'PROCEDURE', 'TASK'):
        ordered.extend(obj['file'] for obj in objects if obj['type'] == kind)
    manifest = {
        'exported_at': datetime.now(ZoneInfo('Europe/Stockholm')).isoformat(timespec='seconds'),
        'source': source, 'schemas': scope['schemas'],
        'counts': dict(sorted(Counter(obj['type'] for obj in objects).items())),
        'objects': objects, 'restore_order': ordered,
    }
    if not requested:
        manifest['excluded_generated_task_count'] = len(excluded)
    # Gather everything successfully before changing the tracked snapshot.
    for filename, text in definitions.items():
        path = ROOT / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
    for obj in previous.get('objects', []):
        filename = obj['file']
        if filename not in definitions:
            path = (ROOT / filename).resolve()
            if not path.is_relative_to(ROOT / 'ddl'):
                raise ValueError('Invalid previous export path: ' + filename)
            path.unlink(missing_ok=True)
    (ROOT / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Exported:', json.dumps(manifest['counts']), flush=True)
    if requested:
        print('Refreshed changed objects:', len(requested), flush=True)
    else:
        print('Excluded generated workflow tasks:', len(excluded), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', help='Snowflake role to use for reading definitions')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--object', dest='items', nargs=2, action='append', metavar=('TYPE', 'NAME'),
                        help='Save only this changed object; repeat for multiple objects')
    action.add_argument('--refresh', action='store_true', help='Explicitly refresh the two maintained app schemas')
    action.add_argument('--build-restore', metavar='FILE', help='Build an offline SQL bundle from the saved app definitions')
    args = parser.parse_args()
    if args.build_restore:
        build_restore(args.build_restore)
    elif args.items or args.refresh:
        export(args.role, args.items, args.refresh)
    else:
        parser.print_help()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        message = str(exc)
        for key in ('SNOWFLAKE_PASSWORD', 'SNOWFLAKE_PASSCODE'):
            secret = os.getenv(key)
            if secret:
                message = message.replace(secret, '[redacted]')
        print('Export failed:', message, file=sys.stderr)
        sys.exit(1)
