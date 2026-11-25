"""Transport classification and safe recovery boundaries; no external calls."""
import errno
import socket
import ssl


def connection_failure(error):
    # Inspect the installed connector's transport errors, never its raw message:
    # messages may contain connection parameters or authentication material.
    from snowflake.connector.errors import OperationalError, ProgrammingError
    if isinstance(error, ProgrammingError) or type(error).__module__.startswith('psycopg'):
        return None
    pending, seen, errors = [error], set(), []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current)); errors.append(current)
        pending.extend(e for e in (current.__cause__, current.__context__, getattr(current, 'reason', None)) if isinstance(e, BaseException))
    if any(isinstance(e, ssl.SSLCertVerificationError) or type(e).__name__ == 'SSLError' for e in errors):
        return None
    for current in errors:
        if isinstance(current, socket.gaierror) or type(current).__name__ == 'NameResolutionError':
            return 'DNS_LOOKUP_FAILED'
        if isinstance(current, TimeoutError) or type(current).__name__ in ('ConnectTimeout', 'ReadTimeout', 'ConnectTimeoutError', 'ReadTimeoutError'):
            return 'CONNECTION_TIMEOUT'
        if isinstance(current, ConnectionError):
            return 'CONNECTION_LOST'
        if isinstance(current, OSError) and current.errno in (errno.ENETDOWN,errno.ENETUNREACH,errno.EHOSTUNREACH,errno.ETIMEDOUT):
            return 'CONNECTION_LOST'
        if type(current).__module__.startswith(('requests.', 'urllib3.', 'snowflake.connector.vendored.')) and type(current).__name__ in ('ConnectionError', 'NewConnectionError', 'ProtocolError'):
            return 'CONNECTION_LOST'
    if isinstance(error, OperationalError) and error.errno in (250001, 250002, 250003, 250005, 251011):
        return 'CONNECTION_UNAVAILABLE'
    return None


def recovery_stage(job):
    """Only resume known failure phases; submitted work is observation-only."""
    if job['state'] != 'UNKNOWN' or job.get('ended_at'):
        return None
    error = job.get('error') or ''
    if not job.get('submit_started_at'):
        if error.endswith(': task creation unavailable') and not any(job.get(k) for k in ('native_group_id', 'native_attempt', 'query_id', 'native_started_at', 'observed_at')):
            return 'CREATING'
    elif error.startswith('Submission/status unavailable:') or error.startswith('No native invocation is visible;'):
        return 'SUBMITTING'
    return None


def retry_delay(count):
    return min(300, 2 ** min(count, 9))
