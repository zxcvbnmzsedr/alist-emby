#!/usr/bin/env python3
"""Query on the workstation; send validated facts and a cover to the NAS via SSH."""
import argparse
import os
import asyncio
import json
from pathlib import Path
import shlex
import subprocess
from import_metadata import fetch_facts, normalize_code
from metadata_artwork import fetch_cover

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('folder')
parser.add_argument('--apply', action='store_true')
parser.add_argument('--skip-cover', action='store_true', help='Import text metadata only')
parser.add_argument('--host', default=os.environ.get('ALIST_EMBY_SSH_HOST'), required=not bool(os.environ.get('ALIST_EMBY_SSH_HOST')))
args = parser.parse_args()
code = normalize_code(args.folder)
root = Path(__file__).parents[1]
candidates = []
try:
    facts, sources, failures = asyncio.run(fetch_facts(code, candidates))
except RuntimeError:
    if not args.apply or args.skip_cover:
        raise
    # No validated facts: keep NFO unchanged and still allow the missing-cover fallback.
    subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', args.host,
                    shlex.join(['cinema-import', args.folder, '--auto-cover', '--scrape-attempted'])], check=True)
    raise SystemExit(0)
payload = {'facts': facts, 'sources': sources, 'failed_sources': failures}
if not args.skip_cover:
    try:
        payload['poster'], cover_failures = asyncio.run(fetch_cover(candidates))
        failures.extend({'stage': 'cover', **failure} for failure in cover_failures)
    except RuntimeError as error:
        failures.append({'stage': 'cover', 'error_type': type(error).__name__})
command = ['cinema-import', args.folder, '--verified-stdin']
if args.apply:
    command.append('--apply')
if args.skip_cover:
    command.append('--skip-cover')
subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', args.host, shlex.join(command)],
               input=json.dumps(payload, ensure_ascii=False).encode(), check=True)
