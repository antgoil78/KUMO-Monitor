from dataclasses import dataclass
import os
import re


@dataclass(frozen=True)
class Settings:
    dsn: str
    namespace: str = 'KUMO_DEV.MONITOR_APP'
    warehouse: str = 'KUMO_ELT_GEN_1'
    role: str = 'KUMO_ADMIN'
    poll_seconds: int = 5
    lease_seconds: int = 120
    max_recovery_checks: int = 6

    def __post_init__(self):
        if self.namespace != 'KUMO_DEV.MONITOR_APP':
            raise ValueError('The task prototype is limited to KUMO_DEV.MONITOR_APP')
        for value in (self.warehouse, self.role):
            if not re.fullmatch(r'[A-Z_][A-Z0-9_$]*', value):
                raise ValueError('Use a single unquoted warehouse/role identifier')
        if not self.dsn or not 1 <= self.poll_seconds <= 60 or self.lease_seconds < 90:
            raise ValueError('Configure PostgreSQL, a 1–60 second poll interval and a lease of at least 90 seconds')

    @classmethod
    def from_environment(cls):
        if os.getenv('KUMO_TASK_PROTOTYPE_ENABLED', '').lower() != 'true':
            raise PermissionError('The PostgreSQL task prototype is disabled')
        if os.getenv('KUMO_ENV', 'DEV').upper() != 'DEV':
            raise PermissionError('The task prototype is DEV only')
        return cls(os.getenv('KUMO_POSTGRES_DSN', ''),
                   warehouse=os.getenv('KUMO_PG_TASK_WAREHOUSE', 'KUMO_ELT_GEN_1').upper(),
                   role=os.getenv('KUMO_PG_TASK_ROLE', 'KUMO_ADMIN').upper(),
                   poll_seconds=int(os.getenv('KUMO_PG_TASK_POLL_SECONDS', '5')))
