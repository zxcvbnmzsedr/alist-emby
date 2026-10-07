#!/usr/bin/env python3
"""Scrape missing NAS covers, fall back to second 3; optionally preview."""
import argparse
import os
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', nargs='?')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--preview', action='store_true', help='Download a preview without publishing')
    modes.add_argument('--list', action='store_true', help='List videos without covers')
    modes.add_argument('--apply-preview', metavar='ID', help='Publish the exact preview already selected')
    parser.add_argument('--time', default='00:00:03', metavar='HH:MM:SS', help='Frame time (default second 3)')
    parser.add_argument('--host', default=os.environ.get('ALIST_EMBY_SSH_HOST'), required=not bool(os.environ.get('ALIST_EMBY_SSH_HOST')))
    parser.add_argument('--remote-state', default=os.environ.get('SCRAPER_STATE_PATH', '/var/lib/alist-emby/scraper'))
    parser.add_argument('--output', type=Path, default=Path(__file__).parents[2] / 'outputs/frame-covers')
    args = parser.parse_args()
    command = ['cinema-import']
    if args.list or args.apply_preview:
        if args.folder:
            parser.error('--list and --apply-preview do not take a folder')
        command += ['--list-missing-covers'] if args.list else ['--apply-preview', args.apply_preview]
    elif args.preview:
        if not args.folder or args.folder.startswith('-'):
            parser.error('--preview requires a media folder')
        command += [args.folder, '--frame', args.time]
    else:
        if args.folder:
            if args.folder.startswith('-'):
                parser.error('Invalid media folder')
            command.append(args.folder)
        command += ['--auto-cover', '--cover-time', args.time]
    remote = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', args.host,
                             shlex.join(command)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if remote.returncode and not remote.stdout:
        raise RuntimeError('NAS command failed: ' + remote.stderr.decode(errors='replace').strip())
    result = json.loads(remote.stdout)
    if result['status'] == 'preview':
        ident = result.get('preview_id', '')
        if not re.fullmatch(r'[a-f0-9]{32}', ident):
            raise ValueError('Unexpected preview ID from NAS')
        args.output.mkdir(parents=True, exist_ok=True)
        local = args.output / f'{ident}.jpg'
        source = f'{args.host}:{args.remote_state.rstrip("/")}/frame-covers/{ident}/poster.jpg'
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', source, str(local)], check=True)
        result['local_preview'] = str(local.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if remote.returncode:
        sys.exit(remote.returncode)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        sys.exit(1)
