#!/usr/bin/env python3
"""Read local batch progress without exposing filenames or credentials."""
import json
import os
from pathlib import Path
from collections import Counter

from batch_media import WORK

root = WORK
inventory = json.loads((root / 'inventory.json').read_text())
state = json.loads((root / 'state.json').read_text()) if (root / 'state.json').exists() else {'jobs': {}}
counts = Counter(state['jobs'].get(i['uid'], {}).get('status', 'pending') for i in inventory['items'])
active = []
for uid, job in state['jobs'].items():
    if job['status'] not in ('complete', 'failed', 'pending'):
        info = {k: job[k] for k in ('folder', 'status', 'progress') if k in job}
        progress = root / 'jobs' / uid / 'private/ffmpeg-progress.txt'
        if job['status'] == 'packaging' and progress.exists():
            latest = dict(line.split('=', 1) for line in progress.read_text().splitlines() if '=' in line)
            info['processed_time'] = latest.get('out_time')
            info['speed'] = latest.get('speed')
        active.append(info)
running = False
if (root / 'runner.pid').exists():
    try:
        os.kill(int((root / 'runner.pid').read_text()), 0)
        running = True
    except (OSError, ValueError):
        pass
print(json.dumps({'total': len(inventory['items']), 'running': running, 'counts': dict(counts),
                  'active': active, 'stop_requested': (root / 'STOP').exists(),
                  'failures': [{'folder': j['folder'], 'error': j.get('error')} for j in state['jobs'].values() if j['status'] == 'failed']},
                 ensure_ascii=False, indent=2))
