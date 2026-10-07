#!/usr/bin/env python3
"""Delete only unchanged originals of completed, freshly audited batch jobs."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import time

import batch_media as batch
from batch_media import WORK, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--source-root', type=Path, required=True, help='Explicit original directory authorized for cleanup')
    parser.add_argument('--refresh-audit', action='store_true', help='Recheck completed cloud publications over SSH')
    parser.add_argument('--host', default=batch.HOST)
    args = parser.parse_args()
    with (WORK / 'runner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inventory = json.loads((WORK / 'inventory.json').read_text())
        state = json.loads((WORK / 'state.json').read_text())
        if args.refresh_audit:
            if not args.host:
                parser.error('--refresh-audit requires --host or ALIST_EMBY_SSH_HOST')
            batch.HOST = args.host
            fresh = []
            for item in inventory['items']:
                if state['jobs'].get(item['uid'], {}).get('status') == 'complete':
                    proof = batch.cloud('audit', item)
                    fresh.append(dict(proof, safe_to_remove_source=proof.get('status') == 'verified'))
            save(WORK / 'cleanup-audit.json', fresh)
        audit_path = WORK / 'cleanup-audit.json'
        if time.time() - audit_path.stat().st_mtime > 900:
            raise ValueError('Fresh cloud audit required before source deletion')
        audits = {a['uid']: a for a in json.loads(audit_path.read_text())}
        inventory = json.loads((WORK / 'inventory.json').read_text())
        state = json.loads((WORK / 'state.json').read_text())
        source_root = Path(inventory['source_root']).resolve()
        if source_root != args.source_root.resolve():
            raise ValueError('Unexpected source root')
        planned = []
        # Validate the entire plan before deleting the first file.
        for item in inventory['items']:
            uid = item['uid']
            if state['jobs'].get(uid, {}).get('status') != 'complete':
                continue
            audit = audits.get(uid, {})
            if not audit.get('safe_to_remove_source'):
                raise ValueError(f'Cloud verification missing for {uid}')
            job = WORK / 'jobs' / uid
            key = job / 'private/key'
            if key.is_symlink() or hashlib.sha256(key.read_bytes()).hexdigest() != audit['key_sha256']:
                raise ValueError(f'Local key backup differs for {uid}')
            source = Path(item['source'])
            if source.is_symlink() or not source.resolve().is_relative_to(source_root) or source.resolve() != source:
                raise ValueError('Refusing source outside the original directory')
            if not source.exists() and state['jobs'][uid].get('source_deleted'):
                continue
            stat = source.stat()
            if not source.is_file() or (stat.st_size, stat.st_mtime_ns) != (item['size'], item['mtime_ns']):
                raise ValueError(f'Source changed for {uid}; retained')
            temporary = []
            for directory in (job / 'packs', job / 'staging'):
                if directory.is_symlink():
                    raise ValueError('Refusing generated-directory symlink')
                if directory.exists():
                    for path in directory.rglob('*'):
                        if path.is_symlink():
                            raise ValueError('Refusing generated-file symlink')
                        if path.is_file():
                            temporary.append(path)
            for name in ('private/ffmpeg.log', 'private/ffmpeg-progress.txt', 'private/verification.log',
                         'private/verify.m3u8', 'transfer.log'):
                path = job / name
                if path.is_symlink():
                    raise ValueError('Refusing log symlink')
                if path.is_file():
                    temporary.append(path)
            planned.append({'uid': uid, 'source': str(source), 'source_bytes': stat.st_size,
                            'temporary': [str(p) for p in temporary],
                            'temporary_bytes': sum(p.stat().st_size for p in temporary),
                            'status': 'planned'})
        report = {'created_at': time.time(), 'applied': args.apply, 'items': planned}
        path = WORK / f'cleanup-{time.time_ns()}.json'
        save(path, report)
        if args.apply:
            for row in planned:
                source = Path(row['source'])
                uid = row['uid']
                item = next(i for i in inventory['items'] if i['uid'] == uid)
                stat = source.stat()
                if (stat.st_size, stat.st_mtime_ns) != (item['size'], item['mtime_ns']):
                    raise ValueError(f'Source changed during cleanup for {uid}')
                # Keep the key, canonical playlist, pack hashes and audit records.
                source.unlink()
                state['jobs'][uid].update(source_deleted=True, source_deleted_at=time.time())
                save(WORK / 'state.json', state)
                for temporary in row['temporary']:
                    Path(temporary).unlink()
                row['status'] = 'deleted'
                save(path, report)
        print(json.dumps({'applied': args.apply, 'originals': len(planned),
                          'source_GiB': round(sum(r['source_bytes'] for r in planned) / 1024**3, 2),
                          'temporary_MiB': round(sum(r['temporary_bytes'] for r in planned) / 1024**2, 2),
                          'report': str(path)}))


if __name__ == '__main__':
    main()
