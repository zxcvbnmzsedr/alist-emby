#!/usr/bin/env python3
"""Serial, resumable video import: local HLS -> AList cloud storage -> verified playback."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
WORK = Path(os.environ.get('ALIST_EMBY_WORK_DIR', str(ROOT / 'work/import')))
HOST = os.environ.get('ALIST_EMBY_SSH_HOST', '')
REMOTE_ROOT = os.environ.get('ALIST_EMBY_REMOTE_ROOT', '/opt/alist-emby')
REMOTE_STATE = os.environ.get('BATCH_STATE_PATH', '/var/lib/alist-emby/batches')
REMOTE_PACKS = os.environ.get('BATCH_PACKS_PATH', '/srv/alist-emby/packs')
VIDEO_ENCODER = os.environ.get('ALIST_EMBY_VIDEO_ENCODER', 'h264_videotoolbox' if sys.platform == 'darwin' else 'libx264')
RESERVE = 5 * 1024**3
NAS_RESERVE = 2 * 1024**3


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.next')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.chmod(0o600)
    temp.replace(path)


def event(stage, uid=None, **extra):
    message = {'time': datetime.now(timezone.utc).isoformat(), 'stage': stage, 'uid': uid, **extra}
    print(json.dumps(message, ensure_ascii=False), flush=True)


def run_logged(command, log_path, **kwargs):
    with log_path.open('ab') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, **kwargs)
    if result.returncode:
        raise RuntimeError(f'Command failed; see {log_path.name} (exit {result.returncode})')


def enhance_catalogue(item, job):
    """Scrape first, then fill artwork if missing, including unrecognised codes."""
    metadata, cover = 'no_recognized_code', 'unavailable'
    if item.get('code'):
        command = ['ssh', '-o', 'BatchMode=yes', HOST, shlex.join(['cinema-import', item['folder'], '--apply'])]
        try:
            run_logged(command, job / 'metadata.log', timeout=180)
            metadata = 'imported'
        except (RuntimeError, subprocess.TimeoutExpired):
            metadata = 'unavailable'
    cover_command = ['cinema-import', item['folder'], '--auto-cover']
    if item.get('code'):
        cover_command.append('--scrape-attempted')
    command = ['ssh', '-o', 'BatchMode=yes', HOST, shlex.join(cover_command)]
    try:
        run_logged(command, job / 'cover.log', timeout=360)
        cover = 'checked'
    except (RuntimeError, subprocess.TimeoutExpired):
        pass  # An unavailable optional cover must not block verified playback.
    return metadata, cover


def cloud(action, item, retries=3):
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=20',
           '-o', 'ServerAliveCountMax=3', HOST,
           shlex.join([REMOTE_ROOT + '/scripts/cloud-command', action, item['uid']])]
    last = None
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, input=json.dumps(item, ensure_ascii=False).encode() if action == 'init' else None,
                                    capture_output=True, timeout=1800)
            data = json.loads(result.stdout)
            if result.returncode or data.get('status') == 'error':
                raise RuntimeError(f'Cloud {action}: {data.get("error_type", "error")}: {data.get("message", "")}')
            return data
        except (ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            last = error
            if attempt + 1 < retries:
                event('retry', item['uid'], action=action, attempt=attempt + 1)
                time.sleep(10 * (attempt + 1))
    raise last


def video_mode(item, preserve_hevc=False):
    video = next(s for s in item['probe']['streams'] if s.get('codec_type') == 'video')
    copy = video['codec_name'] == 'h264' and video.get('pix_fmt') in ('yuv420p', 'yuvj420p', 'nv12')
    if preserve_hevc and video['codec_name'] == 'hevc':
        copy = True
    if item.get('force_transcode'):
        copy = False
    return video, copy


def estimated_package_bytes(item, preserve_hevc=False):
    report_path = WORK / 'jobs' / item['uid'] / 'private/report.json'
    if report_path.exists():
        return sum(p['bytes'] for p in json.loads(report_path.read_text())['packs'])
    video, copy = video_mode(item, preserve_hevc)
    original_rate = int(video.get('bit_rate') or item['probe']['format'].get('bit_rate') or 3_500_000)
    target_rate = min(18_000_000, max(3_500_000, int(original_rate * 1.7)))
    return item['size'] * 1.25 if copy else float(item['probe']['format']['duration']) * (target_rate + 256_000) / 8 * 1.15


def delete_verified_source(item, proof, source_root):
    if proof.get('status') != 'verified' or proof.get('uid') != item['uid']:
        raise ValueError('Fresh cloud publication audit required for source removal')
    key = WORK / 'jobs' / item['uid'] / 'private/key'
    if key.is_symlink() or hashlib.sha256(key.read_bytes()).hexdigest() != proof.get('key_sha256'):
        raise ValueError('Local key backup differs; source retained')
    source = Path(item['source'])
    if source.is_symlink() or source.resolve() != source or not source.resolve().is_relative_to(source_root.resolve()):
        raise ValueError('Source is outside the authorized directory')
    receipt = WORK / 'jobs' / item['uid'] / 'source-cleanup.json'
    if not source.exists() and receipt.exists():
        return True
    stat = source.stat()
    if (stat.st_size, stat.st_mtime_ns) != (item['size'], item['mtime_ns']):
        raise ValueError('Source changed; retained')
    save(receipt, {'uid': item['uid'], 'source': item['source'], 'bytes': stat.st_size, 'status': 'deleting', 'time': time.time()})
    source.unlink()
    save(receipt, {'uid': item['uid'], 'source': item['source'], 'bytes': stat.st_size, 'status': 'deleted', 'time': time.time()})
    return True


def package(item, preserve_hevc=False):
    job = WORK / 'jobs' / item['uid']
    private, output, staging = job / 'private', job / 'packs', job / 'staging'
    source = Path(item['source'])
    stat = source.stat()
    if (stat.st_size, stat.st_mtime_ns) != (item['size'], item['mtime_ns']):
        raise ValueError('Source changed since inventory; original left untouched')
    if (private / 'report.json').exists():
        report = json.loads((private / 'report.json').read_text())
        if report.get('local_decryption_verified') and all((output / p['file']).is_file() and
            (output / p['file']).stat().st_size == p['bytes'] for p in report['packs']):
            return report
    video, copy = video_mode(item, preserve_hevc)
    duration = float(item['probe']['format']['duration'])
    original_rate = int(video.get('bit_rate') or item['probe']['format'].get('bit_rate') or 3_500_000)
    target_rate = min(18_000_000, max(3_500_000, int(original_rate * 1.7)))
    estimated = item['size'] * 1.25 if copy else duration * (target_rate + 256_000) / 8 * 1.15
    if shutil.disk_usage(WORK).free < estimated + RESERVE:
        raise RuntimeError('Insufficient local free space for this package')
    job.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = job / 'job.json'
    if marker.exists() and json.loads(marker.read_text())['uid'] != item['uid']:
        raise ValueError('Generated directory identity differs')
    save(marker, item)
    private.mkdir(exist_ok=True, mode=0o700)
    # These paths are generated by this job only. Never remove source media.
    for generated in (output, staging):
        if generated.is_symlink():
            raise ValueError('Refusing generated directory symlink')
        if generated.exists():
            shutil.rmtree(generated)
        generated.mkdir(mode=0o700)
    key_path = private / 'key'
    key_path.write_bytes(os.urandom(16)); key_path.chmod(0o600)
    command = ['ffmpeg', '-hide_banner', '-v', 'warning', '-nostdin', '-n', '-i', str(source),
               '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn']
    if copy:
        command += ['-c:v', 'copy']
    else:
        command += ['-c:v', VIDEO_ENCODER, '-b:v', str(target_rate), '-maxrate', str(int(target_rate * 1.3)),
                    '-bufsize', str(target_rate * 2), '-pix_fmt', 'yuv420p', '-force_key_frames', 'expr:gte(t,n_forced*6)']
    audio = next((s for s in item['probe']['streams'] if s.get('codec_type') == 'audio'), None)
    command += ['-c:a', 'copy'] if audio is None or audio['codec_name'] == 'aac' else ['-c:a', 'aac', '-b:a', '192k']
    command += ['-progress', str(private / 'ffmpeg-progress.txt'), '-f', 'hls', '-hls_time', '6',
                '-hls_list_size', '0', '-hls_playlist_type', 'vod', '-hls_flags', 'independent_segments',
                '-hls_segment_filename', str(staging / 'part_%05d.ts'), str(staging / 'plain.m3u8')]
    event('packaging', item['uid'], video_mode='copy' if copy else VIDEO_ENCODER)
    run_logged(command, private / 'ffmpeg.log')
    parts, seconds = [], None
    for line in (staging / 'plain.m3u8').read_text().splitlines():
        if line.startswith('#EXTINF:'):
            seconds = float(line.split(':', 1)[1].rstrip(','))
        elif line and not line.startswith('#'):
            if seconds is None or not re.fullmatch(r'part_\d+\.ts', line):
                raise ValueError('Invalid generated HLS segment')
            parts.append((seconds, staging / line))
    if not parts or abs(sum(d for d, _ in parts) - duration) > max(5, duration * .002):
        raise ValueError('Generated playlist duration differs from source')
    manifest = ['#EXTM3U', '#EXT-X-VERSION:4', '#EXT-X-TARGETDURATION:' + str(math.ceil(max(d for d, _ in parts))),
                '#EXT-X-MEDIA-SEQUENCE:0', '#EXT-X-PLAYLIST-TYPE:VOD', '#EXT-X-INDEPENDENT-SEGMENTS']
    packs, records, stream = [], [], None
    try:
        for index, (seconds, plain) in enumerate(parts):
            if stream is None or packs[-1]['duration'] >= 1200 or packs[-1]['bytes'] + plain.stat().st_size + 16 > 512 * 1024**2:
                if stream: stream.close()
                name = f'pack_{len(packs):03d}.ts'
                stream = (output / name).open('wb')
                packs.append({'file': name, 'bytes': 0, 'duration': 0, 'parts': 0})
            iv = os.urandom(16)
            encrypted = subprocess.run(['openssl', 'enc', '-aes-128-cbc', '-K', key_path.read_bytes().hex(),
                                        '-iv', iv.hex(), '-in', str(plain)], capture_output=True)
            if encrypted.returncode:
                raise RuntimeError('Segment encryption failed')
            data = encrypted.stdout
            offset = stream.tell(); stream.write(data)
            pack = packs[-1]
            manifest += [f'#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0x{iv.hex()}', f'#EXTINF:{seconds:.6f},',
                         f'#EXT-X-BYTERANGE:{len(data)}@{offset}', pack['file']]
            records.append({'pack': pack['file'], 'offset': offset, 'length': len(data), 'duration': seconds, 'iv': iv.hex()})
            pack['bytes'] += len(data); pack['duration'] += seconds; pack['parts'] += 1
            plain.unlink()
            if index % 300 == 0: event('encrypting', item['uid'], parts=index + 1, total=len(parts))
    finally:
        if stream: stream.close()
    manifest.append('#EXT-X-ENDLIST')
    text = '\n'.join(manifest) + '\n'
    (private / 'index.m3u8').write_text(text)
    verification = text.replace('URI="key"', f'URI="{key_path}"')
    verification = '\n'.join(str(output / line) if line and not line.startswith('#') else line for line in verification.splitlines()) + '\n'
    (private / 'verify.m3u8').write_text(verification)
    run_logged(['ffmpeg', '-v', 'error', '-xerror', '-allowed_extensions', 'ALL', '-protocol_whitelist', 'file,crypto,data',
                '-i', str(private / 'verify.m3u8'), '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy', '-f', 'null', '-'],
               private / 'verification.log')
    for pack in packs:
        with (output / pack['file']).open('rb') as data:
            pack['sha256'] = hashlib.file_digest(data, 'sha256').hexdigest()
    report = {'uid': item['uid'], 'packs': packs, 'parts': len(records), 'duration': sum(d for d, _ in parts),
              'video_mode': 'copy' if copy else VIDEO_ENCODER, 'local_decryption_verified': True}
    save(private / 'parts.json', records); save(private / 'report.json', report)
    shutil.rmtree(staging)
    return report


def transfer(item, report, free_bytes):
    job = WORK / 'jobs' / item['uid']
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', HOST]
    command = ['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
    remote_dir = REMOTE_PACKS.rstrip('/') + '/' + item['uid']
    # Resume at verified pack boundaries without requiring rsync on the NAS.
    check = '''import json,hashlib,sys,shutil
from pathlib import Path
request=json.load(sys.stdin); root=Path(request['directory']); ready=[]
for pack in request['packs']:
    p=root/pack['file']
    if p.is_file() and p.stat().st_size==pack['bytes']:
        with p.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()==pack['sha256']:ready.append(pack['file'])
print(json.dumps({'ready':ready,'free_bytes':shutil.disk_usage(root).free}))
'''
    result = subprocess.run(ssh + [shlex.join(['python3', '-c', check])],
                            input=json.dumps({'directory': remote_dir, 'packs': report['packs']}).encode(),
                            capture_output=True, check=True, timeout=300)
    capacity = json.loads(result.stdout)
    ready = set(capacity['ready'])
    needed = sum(p['bytes'] for p in report['packs'] if p['file'] not in ready)
    if needed + NAS_RESERVE > capacity['free_bytes']:
        raise RuntimeError('Insufficient NAS free space for missing packs')
    for pack in report['packs']:
        if pack['file'] in ready: continue
        remote_file = remote_dir + '/' + pack['file']
        run_logged(command + [str(job / 'packs' / pack['file']), HOST + ':' + remote_file + '.next'], job / 'transfer.log')
        run_logged(ssh + [shlex.join(['mv', remote_file + '.next', remote_file])], job / 'transfer.log')
    files = [str(job / 'private' / name) for name in ('key', 'index.m3u8', 'parts.json', 'report.json')]
    run_logged(command + files + [f'{HOST}:{REMOTE_STATE.rstrip("/")}/{item["uid"]}/'], job / 'transfer.log')


def main():
    global HOST
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=HOST, required=not bool(HOST))
    parser.add_argument('--limit', type=int)
    parser.add_argument('--preserve-hevc', action='store_true')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--delete-completed-sources', action='store_true', help='Delete unchanged originals only after a fresh cloud audit')
    args = parser.parse_args(); HOST = args.host
    WORK.mkdir(exist_ok=True, parents=True, mode=0o700)
    lock = (WORK / 'runner.lock').open('a')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('Batch runner is already active')
    (WORK / 'runner.pid').write_text(str(os.getpid()))
    inventory = json.loads((WORK / 'inventory.json').read_text())
    state_path = WORK / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'jobs': {}}
    # Recover jobs already occupying the NAS before adding any new packages.
    items = sorted(inventory['items'], key=lambda item: (
        not (WORK / 'jobs' / item['uid'] / 'private/report.json').exists(),
        not video_mode(item)[1], item['size']))
    completed = 0
    attempted = 0
    for item in items:
        uid = item['uid']
        job_state = state['jobs'].setdefault(uid, {'status': 'pending', 'folder': item['folder']})
        if job_state['status'] == 'complete' or (job_state['status'] == 'failed' and not args.retry_failed):
            continue
        if (WORK / 'STOP').exists():
            event('stopped_between_videos'); break
        attempted += 1
        def update(status, **extra):
            job_state.update(status=status, updated_at=time.time(), **extra)
            state['updated_at'] = time.time(); save(state_path, state)
            event(status, uid, **extra)
        try:
            update('preparing')
            ready = cloud('init', item)
            remote_published = job_state.get('published', False)
            if not remote_published:
                if not job_state.get('uploaded', False):
                    needed = max(0, estimated_package_bytes(item, args.preserve_hevc) - ready.get('resident_bytes', 0))
                    if needed + NAS_RESERVE > ready['free_bytes']:
                        raise RuntimeError('Insufficient NAS free space before packaging')
                    update('packaging')
                    report = package(item, args.preserve_hevc)
                    update('transferring', bytes=sum(p['bytes'] for p in report['packs']))
                    transfer(item, report, ready['free_bytes'])
                    cloud('upload', item)
                    update('uploading')
                    deadline = time.time() + 24 * 3600
                    while True:
                        progress = cloud('status', item)
                        if progress['status'] == 'failed':
                            raise RuntimeError('AList cloud upload task failed; generated files retained')
                        if progress['status'] == 'uploaded': break
                        if time.time() > deadline: raise TimeoutError('Cloud upload exceeded 24 hours')
                        update('uploading', progress=progress['progress'])
                        time.sleep(20)
                    update('uploaded', uploaded=True)
                update('verifying')
                cloud('verify', item)
                cloud('publish', item)
                update('published', published=True)
            # Optional catalogue enhancement must never block valid playback.
            metadata, cover = enhance_catalogue(item, WORK / 'jobs' / uid)
            cloud('cleanup', item)
            packs = WORK / 'jobs' / uid / 'packs'
            if packs.is_dir() and not packs.is_symlink():
                shutil.rmtree(packs)
            source_deleted = job_state.get('source_deleted', False)
            if args.delete_completed_sources and not source_deleted:
                proof = cloud('audit', item)
                source_deleted = delete_verified_source(item, proof, Path(inventory['source_root']))
            if source_deleted and item.get('supersedes_uid'):
                previous = WORK / 'jobs' / item['supersedes_uid']
                identity = json.loads((previous / 'job.json').read_text())
                if identity['source'] != item['source']:
                    raise ValueError('Repair source identity differs')
                previous_packs = previous / 'packs'
                if previous_packs.is_dir() and not previous_packs.is_symlink():
                    shutil.rmtree(previous_packs)
            update('complete', metadata=metadata, cover=cover, source_deleted=source_deleted)
            completed += 1
        except Exception as error:
            update('failed', error_type=type(error).__name__, error=str(error)[:240])
            # Stop on the first unresolved failure, instead of accumulating more packages.
            event('paused_for_recovery'); break
        if args.limit and attempted >= args.limit: break
    counts = {}
    for item in inventory['items']:
        status = state['jobs'].get(item['uid'], {}).get('status', 'pending')
        counts[status] = counts.get(status, 0) + 1
    state['run_finished_at'] = time.time(); save(state_path, state)
    event('run_finished', counts=counts)


if __name__ == '__main__':
    main()
