#!/usr/bin/env python3
"""Probe an explicitly selected media directory into a private resumable inventory."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess

from batch_media import WORK, save

EXTENSIONS = {'.mp4', '.mkv', '.mov', '.m4v', '.avi', '.webm', '.ts'}


def make_inventory(source_root):
    root = Path(source_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Source root must be a directory')
    items = []
    for source in sorted(root.rglob('*')):
        if source.suffix.lower() not in EXTENSIONS or not source.is_file():
            continue
        if source.is_symlink() or source.resolve() != source:
            raise ValueError('Media symlinks are not accepted')
        stat = source.stat()
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)],
                                capture_output=True, check=True, timeout=120)
        probe = json.loads(result.stdout)
        if not any(s.get('codec_type') == 'video' for s in probe.get('streams', [])):
            raise ValueError('Input contains no video stream')
        if float(probe.get('format', {}).get('duration', 0)) <= 0:
            raise ValueError('Input has no valid duration')
        if (source.stat().st_size, source.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise ValueError('Source changed while probing')
        uid = hashlib.sha256(f'{source}\0{stat.st_size}\0{stat.st_mtime_ns}'.encode()).hexdigest()[:12]
        match = re.search(r'(?<![A-Za-z0-9])([A-Za-z]{2,10})[-_](\d{2,6})(?!\d)', source.stem)
        code = f'{match[1].upper()}-{match[2]}' if match else None
        folder = (code.replace('-', '_') if code else 'VIDEO') + '_' + uid
        items.append(dict(uid=uid, folder=folder, code=code, source=str(source), size=stat.st_size,
                          mtime_ns=stat.st_mtime_ns, probe=probe))
    return {'source_root': str(root), 'items': items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_root', type=Path)
    parser.add_argument('--replace', action='store_true', help='Explicitly replace the existing inventory; job records remain')
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (WORK / 'runner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = WORK / 'inventory.json'
        if path.exists() and not args.replace:
            parser.error('Inventory exists; use it to resume, or explicitly select --replace')
        inventory = make_inventory(args.source_root)
        save(path, inventory)
    print(json.dumps({'videos': len(inventory['items']), 'inventory': str(path)}))


if __name__ == '__main__':
    main()
