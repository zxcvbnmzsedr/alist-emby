"""Shared configuration for explicitly invoked live checks on the AList host."""
import os
from pathlib import Path
import sqlite3

from server import Bridge, LocalAccounts


def configured_bridge(require_accounts=False):
    required = ['CATALOG_PATH', 'STATE_PATH', 'PUBLIC_ORIGIN']
    if require_accounts:
        required += ['ALIST_DATABASE', 'ALIST_CONFIG']
    if any(not os.environ.get(key) for key in required):
        raise ValueError('Configure ' + ', '.join(required) + ' before running live verification')
    accounts = LocalAccounts(os.environ['ALIST_DATABASE'], os.environ['ALIST_CONFIG']) if os.environ.get('ALIST_DATABASE') and os.environ.get('ALIST_CONFIG') else None
    return Bridge(os.environ['CATALOG_PATH'], os.environ['STATE_PATH'], os.environ['PUBLIC_ORIGIN'],
                  os.environ.get('ALIST_ORIGIN', 'http://127.0.0.1:5244'), accounts=accounts)


def upstream_token():
    token = os.environ.get('ALIST_TOKEN')
    if token:
        return token
    with sqlite3.connect(Path(os.environ['ALIST_DATABASE']).resolve().as_uri() + '?mode=ro', uri=True) as db:
        return db.execute("SELECT value FROM x_setting_items WHERE key='token'").fetchone()[0]
