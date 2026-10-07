#!/usr/bin/env python3
"""Server half of the resumable media import. Credentials stay on the NAS."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

STATE = Path(os.environ.get('BATCH_STATE_PATH', '/var/lib/alist-emby/batches'))
PACKS = Path(os.environ.get('BATCH_PACKS_PATH', '/srv/alist-emby/packs'))
MEDIA = Path(os.environ.get('MEDIA_ROOT', '/m3u8'))
PUBLIC = Path(os.environ.get('CATALOG_OUTPUT', '/srv/alist-emby/www/cinema'))
CLOUD_ROOT = os.environ.get('ALIST_SEGMENT_ROOT', '/cloud/raw').rstrip('/')
LOCAL_PACK_ROOT = os.environ.get('ALIST_LOCAL_PACK_ROOT', '/local/packs').rstrip('/')
ALIST_ORIGIN = os.environ.get('ALIST_ORIGIN', 'http://127.0.0.1:5244').rstrip('/')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cinema/scripts'))
from build_catalog import atomic_write, build_catalog


def save(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode())
    path.chmod(0o600)


class Cloud:
    def __init__(self):
        self.token = os.environ.get('ALIST_TOKEN', '')
        if not self.token:
            database = os.environ.get('ALIST_DATABASE')
            if not database:
                raise ValueError('Set ALIST_TOKEN or ALIST_DATABASE on the AList server')
            with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
                self.token = db.execute('select value from x_setting_items where key=?', ('token',)).fetchone()[0]

    def api(self, path, data=None):
        req = urllib.request.Request(ALIST_ORIGIN + '/api/' + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': self.token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=90) as response:
            result = json.load(response)
        if result.get('code') != 200:
            raise RuntimeError(f'AList {path}: status {result.get("code")}')
        return result.get('data')

    def listing(self, path):
        return self.api('fs/list', {'path': path, 'password': '', 'refresh': True,
                                  'page': 1, 'per_page': 1000})['content'] or []

    def signed(self, path, prefix='d'):
        obj = self.api('fs/get', {'path': path, 'password': ''})
        if prefix == 'd' and not obj.get('sign'):
            raise RuntimeError('Expected authenticated file signature')
        return '/' + prefix + urllib.parse.quote(path, safe='/') + (
            '?sign=' + urllib.parse.quote(obj['sign'], safe='') if obj.get('sign') else '')


def initialize(uid, item, api):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', item['folder']):
        raise ValueError('Invalid folder')
    base, output = STATE / uid, PACKS / uid
    if base.is_symlink() or output.is_symlink():
        raise ValueError('Refusing symlink job directory')
    if (base / 'job.json').exists():
        previous = json.loads((base / 'job.json').read_text())
        if any(previous[k] != item[k] for k in ('uid', 'folder', 'size', 'mtime_ns')):
            raise ValueError('Job identity changed')
    else:
        if (MEDIA / item['folder']).exists():
            raise ValueError('Playback directory already exists')
        if item['folder'] in {x['name'] for x in api.listing(CLOUD_ROOT)}:
            raise ValueError('Cloud destination already exists')
        base.mkdir(parents=True, mode=0o700)
        save(base / 'job.json', item)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {'status': 'ready', 'free_bytes': shutil.disk_usage(output).free,
            'resident_bytes': sum(p.stat().st_size for p in output.glob('pack_*.ts') if p.is_file() and not p.is_symlink())}


def get_job(uid):
    base, output = STATE / uid, PACKS / uid
    item = json.loads((base / 'job.json').read_text())
    report = json.loads((base / 'report.json').read_text())
    if not report.get('local_decryption_verified') or report.get('uid') != uid:
        raise ValueError('Package has not passed local validation')
    return base, output, item, report


def upload(uid, api):
    base, output, item, report = get_job(uid)
    for pack in report['packs']:
        path = output / pack['file']
        if path.stat().st_size != pack['bytes']:
            raise ValueError('Transferred pack size differs')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != pack['sha256']:
                raise ValueError('Transferred pack checksum differs')
    destination = CLOUD_ROOT + '/' + item['folder']
    if item['folder'] not in {x['name'] for x in api.listing(CLOUD_ROOT)}:
        api.api('fs/mkdir', {'path': destination})
    # Refresh the created directory before copying; avoid concurrent mkdir races.
    api.listing(destination)
    journal = base / 'sequential-upload.json'
    if not journal.exists():
        save(journal, {'current': None, 'attempts': {}})
    return status(uid, api)


def status(uid, api):
    base, output, item, report = get_job(uid)
    destination = CLOUD_ROOT + '/' + item['folder']
    journal_path = base / 'sequential-upload.json'
    journal = json.loads(journal_path.read_text())
    tasks = {}
    for kind in ('undone', 'done'):
        for task in api.api('admin/task/copy/' + kind) or []:
            tasks[task['id']] = {**task, 'finished': kind == 'done'}
    current = journal.get('current')
    active = tasks.get(current['id']) if current else None
    # A network interruption between task submission and journal write must not duplicate an active copy.
    if not current:
        owned = [t for t in tasks.values() if not t['finished'] and
                 LOCAL_PACK_ROOT + '/' + uid in t.get('name', '')]
        if owned:
            return {'status': 'uploading', 'progress': 0}
    files = {f['name']: f for f in api.listing(destination)}
    present = {p['file'] for p in report['packs'] if files.get(p['file'], {}).get('size') == p['bytes']}
    total = sum(p['bytes'] for p in report['packs'])
    done_bytes = sum(p['bytes'] for p in report['packs'] if p['file'] in present)
    if active and not active['finished']:
        pack = next(p for p in report['packs'] if p['file'] == current['file'])
        partial = 0 if pack['file'] in present else pack['bytes'] * active.get('progress', 0) / 100
        return {'status': 'uploading', 'progress': round(100 * (done_bytes + partial) / total, 1)}
    if len(present) == len(report['packs']):
        return {'status': 'uploaded', 'progress': 100}
    missing = next(p for p in report['packs'] if p['file'] not in present)
    attempts = journal['attempts'].get(missing['file'], 0)
    if attempts >= 3:
        return {'status': 'failed', 'progress': round(100 * done_bytes / total, 1)}
    submitted = api.api('fs/copy', {'src_dir': LOCAL_PACK_ROOT + '/' + uid,
                                  'dst_dir': destination, 'names': [missing['file']]})
    if len(submitted.get('tasks', [])) != 1:
        raise RuntimeError('Expected one cloud copy task')
    journal['current'] = {'id': submitted['tasks'][0]['id'], 'file': missing['file']}
    journal['attempts'][missing['file']] = attempts + 1
    save(journal_path, journal)
    return {'status': 'uploading', 'progress': round(100 * done_bytes / total, 1)}


def audit(uid, api):
    base, output, item, report = get_job(uid)
    target = MEDIA / item['folder']
    published = json.loads((base / 'published.json').read_text())
    checks = json.loads((base / 'cloud-verification.json').read_text())
    marker = json.loads((target / '.cinema-import.json').read_text())
    if published.get('uid') != uid or not published.get('verified') or marker.get('uid') != uid:
        raise ValueError('Publication identity mismatch')
    if len(checks) != 2 * len(report['packs']) or not all(c.get('decode_ok') and c.get('http') == 206 for c in checks):
        raise ValueError('Missing cloud decoding checks')
    if (target / 'key').read_bytes() != (base / 'key').read_bytes():
        raise ValueError('Published key differs')
    if item['folder'] not in {v['id'] for v in json.loads((PUBLIC / 'catalog.json').read_text())['videos']}:
        raise ValueError('Film is missing from the published catalogue')
    files = {f['name']: f for f in api.listing(CLOUD_ROOT + '/' + item['folder'])}
    if any(files.get(p['file'], {}).get('size') != p['bytes'] for p in report['packs']):
        raise ValueError('Cloud pack is missing or has changed size')
    return {'status': 'verified', 'uid': uid, 'key_sha256': hashlib.sha256((base / 'key').read_bytes()).hexdigest()}


def verify(uid, api):
    base, output, item, report = get_job(uid)
    if (base / 'cloud-verification.json').exists():
        return {'status': 'verified'}
    cloud = CLOUD_ROOT + '/' + item['folder']
    api.listing(CLOUD_ROOT)
    files = {x['name']: x for x in api.listing(cloud)}
    records = json.loads((base / 'parts.json').read_text())
    checks = []
    for pack in report['packs']:
        if files.get(pack['file'], {}).get('size') != pack['bytes']:
            raise ValueError('Cloud pack size differs')
        parts = [r for r in records if r['pack'] == pack['file']]
        url = ALIST_ORIGIN + api.signed(cloud + '/' + pack['file'], 'p')
        for part in (parts[0], parts[-1]):
            start, length = part['offset'], part['length']
            req = urllib.request.Request(url, headers={'Range': f'bytes={start}-{start + length - 1}'})
            with urllib.request.urlopen(req, timeout=150) as response:
                if response.status != 206 or response.headers.get('Content-Range') != f'bytes {start}-{start + length - 1}/{pack["bytes"]}':
                    raise ValueError('Cloud does not support the expected byte range')
                data = response.read(length + 1)
            with (output / pack['file']).open('rb') as stream:
                stream.seek(start)
                if data != stream.read(length):
                    raise ValueError('Cloud ciphertext differs')
            plain = subprocess.run(['openssl', 'enc', '-d', '-aes-128-cbc', '-K',
                (base / 'key').read_bytes().hex(), '-iv', part['iv']], input=data, capture_output=True)
            if plain.returncode:
                raise ValueError('Cloud sample did not decrypt')
            decoded = subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', 'pipe:0', '-f', 'null', '-'],
                                     input=plain.stdout, capture_output=True, timeout=90)
            if decoded.returncode:
                raise ValueError('Cloud sample did not decode')
            checks.append({'pack': pack['file'], 'offset': start, 'bytes': length, 'http': 206, 'decode_ok': True})
    save(base / 'cloud-verification.json', checks)
    return {'status': 'verified', 'samples': len(checks)}


def publish(uid, api):
    base, output, item, report = get_job(uid)
    if not (base / 'cloud-verification.json').exists():
        raise ValueError('Cloud verification required')
    target = MEDIA / item['folder']
    marker = target / '.cinema-import.json'
    if target.exists() and (not marker.is_file() or json.loads(marker.read_text()).get('uid') != uid):
        raise ValueError('Refusing to replace an unrelated playback directory')
    target.mkdir(exist_ok=True, mode=0o700)
    save(marker, {'uid': uid, 'source_size': item['size']})
    key = base / 'key'
    if (target / 'key').exists() and (target / 'key').read_bytes() != key.read_bytes():
        raise ValueError('Published key differs')
    atomic_write(target / 'key', key.read_bytes())
    (target / 'key').chmod(0o600)
    if not (target / 'movie.nfo').exists():
        movie = ET.Element('movie')
        ET.SubElement(movie, 'title').text = (item.get('code') or '').replace('_', '-') or Path(item['source']).stem
        atomic_write(target / 'movie.nfo', ET.tostring(movie, encoding='utf-8', xml_declaration=True))
    api.listing('/m3u8')
    api.listing('/m3u8/' + item['folder'])
    key_url = api.signed('/m3u8/' + item['folder'] + '/key')
    cloud = CLOUD_ROOT + '/' + item['folder']
    links = {p['file']: api.signed(cloud + '/' + p['file'], 'p') for p in report['packs']}
    text = (base / 'index.m3u8').read_text().replace('URI="key"', f'URI="{key_url}"')
    text = '\n'.join(links.get(line, line) for line in text.splitlines()) + '\n'
    scraper_state = Path(os.environ.get('SCRAPER_STATE_PATH', '/var/lib/alist-emby/scraper'))
    scraper_state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (scraper_state / 'import.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        atomic_write(target / 'index.m3u8', text.encode())
        (target / 'index.m3u8').chmod(0o600)
        build_catalog(MEDIA, PUBLIC)
    api.listing('/m3u8/' + item['folder'])
    url = api.signed('/m3u8/' + item['folder'] + '/index.m3u8')
    with urllib.request.urlopen(ALIST_ORIGIN + url, timeout=30) as response:
        if response.read() != text.encode():
            raise ValueError('Published playlist readback differs')
    for filename in ('key', 'index.m3u8'):
        unsigned = ALIST_ORIGIN + '/d' + urllib.parse.quote('/m3u8/' + item['folder'] + '/' + filename)
        try:
            with urllib.request.urlopen(unsigned, timeout=30) as response:
                body = response.read()
                if body == (target / filename).read_bytes() or json.loads(body).get('code') not in (401, 403):
                    raise ValueError('Unsigned private file access was not denied')
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403):
                raise
    save(base / 'published.json', {'uid': uid, 'folder': item['folder'], 'verified': True, 'published_at': time.time()})
    return {'status': 'published', 'folder': item['folder']}


def cleanup(uid):
    base, output, item, report = get_job(uid)
    if not (base / 'published.json').exists() or not (base / 'cloud-verification.json').exists():
        raise ValueError('Refusing cleanup of unverified media')
    for pack in report['packs']:
        if not re.fullmatch(r'pack_\d+\.ts', pack['file']):
            raise ValueError('Unexpected generated filename')
        (output / pack['file']).unlink(missing_ok=True)
    return {'status': 'cleaned', 'free_bytes': shutil.disk_usage(PACKS).free}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['init', 'upload', 'status', 'verify', 'publish', 'cleanup', 'audit'])
    parser.add_argument('uid')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-f0-9]{12}', args.uid):
        raise ValueError('Invalid job ID')
    api = Cloud()
    STATE.mkdir(exist_ok=True, parents=True, mode=0o700)
    with (STATE / (args.uid + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'init':
            result = initialize(args.uid, json.load(sys.stdin), api)
        elif args.action == 'cleanup':
            result = cleanup(args.uid)
        else:
            result = globals()[args.action](args.uid, api)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never print request objects, signed URLs, cookies or encryption keys.
        print(json.dumps({'status': 'error', 'error_type': type(error).__name__, 'message': str(error)[:160]}))
        sys.exit(1)
