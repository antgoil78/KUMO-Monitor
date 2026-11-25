"""Prepare the DEV PostgreSQL service SQL; never connect or replace storage."""
import argparse
from pathlib import Path
import re
from string import Template

from render_service import identifier, SHARED_COMPUTE_POOL


def render(image, compute_pool=SHARED_COMPUTE_POOL, owner_role='KUMO_ADMIN'):
    if not re.fullmatch(r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/kumo-monitor-postgres:[A-Za-z0-9_.-]+', image):
        raise ValueError('Use the versioned kumo-monitor-postgres image in the Snowflake repository')
    pool=identifier(compute_pool);owner=identifier(owner_role)
    spec=Template(Path(__file__).with_name('postgres.yaml.template').read_text()).substitute(POSTGRES_IMAGE=image)
    return (f'USE ROLE {owner};\n'
            '-- The credential is provisioned separately; no password belongs in this file.\n'
            '-- CREATE deliberately fails if the database service already exists. Never replace its volume.\n'
            'CREATE SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_POSTGRES_SERVICE\n'
            f'  IN COMPUTE POOL {pool}\n  FROM SPECIFICATION $$\n{spec}$$\n'
            '  MIN_INSTANCES=1 MAX_INSTANCES=1;\n'
            'GRANT SERVICE ROLE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_POSTGRES_SERVICE!ALL_ENDPOINTS_USAGE '
            f'TO ROLE {owner};\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--compute-pool',default=SHARED_COMPUTE_POOL)
    parser.add_argument('--owner-role',default='KUMO_ADMIN')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.write_text(render(args.image,args.compute_pool,args.owner_role))
