"""Scrape missing covers first; fall back to second 3 or manual HLS previews."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from build_catalog import atomic_write, build_catalog, regular_within
from metadata_artwork import inspect_image

EXTENSIONS = ('jpg', 'jpeg', 'png', 'webp', 'avif')
DEFAULT_FRAME_TIME = '00:00:03'


def seconds(value):
    parts = str(value).split(':')
    if not 1 <= len(parts) <= 3 or any(not re.fullmatch(r'\d+(?:\.\d+)?', p) for p in parts):
        raise ValueError('Time must be seconds, MM:SS or HH:MM:SS')
    numbers = [float(p) for p in parts]
    if len(parts) > 1 and (any('.' in p for p in parts[:-1]) or any(n >= 60 for n in numbers[1:])):
        raise ValueError('Invalid clock time')
    value = sum(n * 60 ** i for i, n in enumerate(reversed(numbers)))
    if not math.isfinite(value) or value < 0:
        raise ValueError('Time must be finite and nonnegative')
    return value


def media_folder(root, name):
    root = Path(root).resolve()
    if not name or name in ('.', '..') or any(c in name for c in '/\\\x00\r\n'):
        raise ValueError('Provide one media directory name')
    folder = root / name
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != root:
        raise ValueError('Invalid media directory')
    if not regular_within(folder / 'index.m3u8', root):
        raise ValueError('Missing regular index.m3u8')
    return folder


def cover_exists(folder):
    paths = [folder / f'{name}.{ext}'
             for name in ('poster', 'folder', f'{folder.name}-poster', 'index-poster')
             for ext in EXTENSIONS]
    if any(p.is_symlink() for p in paths):
        raise ValueError('Refusing symbolic-link cover')
    return any(p.is_file() for p in paths)


def playlist_source(folder):
    raw = (folder / 'index.m3u8').read_bytes()
    if len(raw) > 8_000_000 or not raw.startswith(b'#EXTM3U'):
        raise ValueError('Invalid or oversized playlist')
    text = raw.decode('utf-8-sig')
    durations = [float(n) for n in re.findall(r'^#EXTINF:([0-9.]+)', text, re.M)]
    duration = sum(durations)
    if not durations or not math.isfinite(duration) or duration <= 0 or '#EXT-X-ENDLIST' not in text:
        raise ValueError('Expected a finite video playlist')
    return text, duration, hashlib.sha256(raw).hexdigest()


def list_missing(root):
    videos = []
    for path in sorted(Path(root).iterdir()):
        if not path.is_dir() or path.is_symlink() or not (path / 'index.m3u8').is_file():
            continue
        folder = media_folder(root, path.name)
        if not cover_exists(folder):
            _, duration, _ = playlist_source(folder)
            videos.append({'folder': folder.name, 'duration': round(duration, 2)})
    return {'status': 'missing_covers', 'count': len(videos), 'videos': videos}


class Alist:
    origin = 'http://127.0.0.1:5244'

    def __init__(self):
        self.origin = os.environ.get('ALIST_ORIGIN', self.origin).rstrip('/')
        self.token = os.environ.get('ALIST_TOKEN', '')
        if not self.token:
            database = os.environ.get('ALIST_DATABASE')
            if not database:
                raise RuntimeError('Configure ALIST_TOKEN or a readable ALIST_DATABASE for frame extraction')
            with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
                row = db.execute("SELECT value FROM x_setting_items WHERE key='token'").fetchone()
            if not row or not row[0]:
                raise RuntimeError('AList credentials unavailable')
            self.token = row[0]

    def signed(self, path, prefix):
        request = Request(self.origin + '/api/fs/get',
                          data=json.dumps({'path': path, 'password': ''}).encode(),
                          headers={'Authorization': self.token, 'Content-Type': 'application/json'})
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result.get('code') != 200 or not result.get('data'):
            raise RuntimeError('AList could not resolve playback file')
        signature = result['data'].get('sign', '')
        if prefix == 'd' and not signature:
            raise RuntimeError('AList private file signature unavailable')
        return self.origin + '/' + prefix + quote(path, safe='/') + (
            '?sign=' + quote(signature, safe='') if signature else '')


def refresh_playlist(text, folder, api):
    """Refresh known AList paths only, preserving encryption IVs and byte ranges."""
    cache = {}

    def resolve(uri, key=False):
        parsed = urlsplit(uri)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError('Expected an existing root-relative AList URI')
        path = unquote(parsed.path)
        if key and path == 'key':
            path = f'/d/m3u8/{folder}/key'
        if key:
            if path != f'/d/m3u8/{folder}/key':
                raise ValueError('Key URI is outside this video')
            prefix, target = 'd', path[2:]
        else:
            remote_root = os.environ.get('ALIST_SEGMENT_ROOT', '/cloud/raw').rstrip('/')
            if not remote_root.startswith('/') or any(p in ('.', '..') for p in remote_root.split('/')):
                raise ValueError('Invalid ALIST_SEGMENT_ROOT')
            bases = [('p', f'/p{remote_root}/{folder}/'), ('d', f'/d/m3u8/{folder}/')]
            if not path.startswith('/'):
                path = f'/d/m3u8/{folder}/' + path
            match = next(((prefix, base) for prefix, base in bases if path.startswith(base)), None)
            if match is None or not re.fullmatch(r'[\w.-]+\.(ts|m4s|mp4)', path[len(match[1]):]):
                raise ValueError('Segment must belong to this video in the configured storage root')
            prefix, target = match[0], path[2:]

        cache_key = (prefix, target)
        if cache_key not in cache:
            cache[cache_key] = api.signed(target, prefix)
        return cache[cache_key]

    lines = []
    for line in text.splitlines():
        if line.startswith('#EXT-X-KEY:'):
            method = re.search(r'(?:^|,)METHOD=([^,]+)', line.split(':', 1)[1])
            if not method or method[1] not in ('AES-128', 'NONE'):
                raise ValueError('Unsupported encryption method')
            if method[1] == 'AES-128' and not re.search(r'URI="[^"]+"', line):
                raise ValueError('Missing encryption key URI')
            line = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + resolve(m[1], key=True) + '"', line)
        elif 'URI=' in line or line.startswith(('#EXT-X-STREAM-INF', '#EXT-X-MEDIA:', '#EXT-X-MAP:')):
            raise ValueError('Expected a media playlist without external resources')
        elif line and not line.startswith('#'):
            line = resolve(line)
        lines.append(line)
    return '\n'.join(lines) + '\n'


def preview_frame(name, time, root, state_dir, api=None, timeout=120):
    folder = media_folder(root, name)
    if cover_exists(folder):
        return {'status': 'preserved_existing', 'folder': name}
    text, duration, digest = playlist_source(folder)
    position = seconds(time)
    if position >= duration:
        raise ValueError(f'Time must be before video duration ({duration:.2f} seconds)')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is not installed')
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    previews = state_dir / 'frame-covers'
    if previews.is_symlink():
        raise ValueError('Refusing symbolic-link preview directory')
    previews.mkdir(exist_ok=True, mode=0o700)
    ident = uuid.uuid4().hex
    run = previews / ident
    run.mkdir(mode=0o700)
    try:
        playlist = refresh_playlist(text, name, api or Alist())
        with tempfile.TemporaryDirectory(prefix='decode-', dir=run) as tmp:
            manifest = Path(tmp) / 'preview.m3u8'
            manifest.write_text(playlist)
            manifest.chmod(0o600)
            command = [ffmpeg, '-hide_banner', '-v', 'error', '-nostdin', '-n',
                       '-allowed_extensions', 'ALL', '-protocol_whitelist', 'file,http,https,tcp,tls,crypto',
                       '-rw_timeout', '15000000', '-ss', str(position), '-i', str(manifest),
                       '-map', '0:v:0', '-frames:v', '1', '-an', '-vf', 'scale=960:-2',
                       '-q:v', '2', '-threads', '2', str(run / 'poster.jpg')]
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
            if result.returncode or not (run / 'poster.jpg').is_file():
                raise RuntimeError('Frame extraction failed; check playback or choose another time')
        image = run / 'poster.jpg'
        image.chmod(0o600)
        details = inspect_image(image.read_bytes())
        report = {'status': 'preview', 'preview_id': ident, 'folder': name,
                  'seconds': position, 'duration': round(duration, 2),
                  'playlist_sha256': digest, 'image': details, 'preview_path': str(image)}
        atomic_write(run / 'report.json', (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode())
        (run / 'report.json').chmod(0o600)
        return report
    except Exception:
        shutil.rmtree(run)
        raise


def publish_preview(ident, root, output, state_dir):
    if not re.fullmatch(r'[a-f0-9]{32}', ident):
        raise ValueError('Invalid preview ID')
    state_dir, output = Path(state_dir), Path(output)
    run = state_dir / 'frame-covers' / ident
    if run.is_symlink() or not regular_within(run / 'report.json', state_dir) or not regular_within(run / 'poster.jpg', state_dir):
        raise ValueError('Preview unavailable or unsafe')
    report = json.loads((run / 'report.json').read_text())
    if report.get('preview_id') != ident:
        raise ValueError('Preview identity does not match')
    raw = (run / 'poster.jpg').read_bytes()
    if inspect_image(raw) != report['image']:
        raise ValueError('Preview image changed; create a new preview')
    with (state_dir / 'import.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        folder = media_folder(root, report['folder'])
        if cover_exists(folder):
            return {'status': 'preserved_existing', 'folder': folder.name, 'preview_id': ident}
        if playlist_source(folder)[2] != report['playlist_sha256']:
            raise ValueError('Video playlist changed; create a new preview')
        destination = folder / 'poster.jpg'
        catalog_path = output / 'catalog.json'
        old_catalog = catalog_path.read_bytes() if catalog_path.exists() else None
        if old_catalog is not None:
            atomic_write(run / 'previous-catalog.json', old_catalog)
            (run / 'previous-catalog.json').chmod(0o600)
        try:
            atomic_write(destination, raw)
            catalog = build_catalog(root, output)
        except Exception:
            destination.unlink(missing_ok=True)
            if old_catalog is not None:
                atomic_write(catalog_path, old_catalog)
            else:
                catalog_path.unlink(missing_ok=True)
            raise
        report.update(status='published', poster_path=str(destination), video_count=len(catalog['videos']))
        atomic_write(run / 'report.json', (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode())
        (run / 'report.json').chmod(0o600)
        return report


def ensure_cover(name, root, output, state_dir, time=DEFAULT_FRAME_TIME, scrape=True):
    """No credential reads, decoding, writes or reindexing when artwork exists."""
    folder = media_folder(root, name)
    if cover_exists(folder):
        return {'status': 'preserved_existing', 'folder': name}
    if scrape:
        from import_metadata import scrape_for_cover
        scraping = scrape_for_cover(name, root, output, state_dir)
        if cover_exists(folder):
            return {'status': 'published', 'folder': name, 'cover_source': 'scraper', 'scraping': scraping}
    else:
        scraping = {'status': 'already_attempted'}
    preview = preview_frame(name, time, root, state_dir)
    if preview['status'] != 'preview':
        return preview
    result = publish_preview(preview['preview_id'], root, output, state_dir)
    result.update(cover_source='frame', scraping=scraping)
    return result


def fill_missing(root, output, state_dir, time=DEFAULT_FRAME_TIME, progress=None):
    """Serial, resumable sweep. Failures remain missing and can be retried."""
    root, state_dir = Path(root), Path(state_dir)
    folders = [p for p in sorted(root.iterdir()) if p.is_dir() and not p.is_symlink()
               and (p / 'index.m3u8').is_file()]
    missing = [p for p in folders if not cover_exists(media_folder(root, p.name))]
    report = {'status': 'auto_cover_complete', 'seconds': seconds(time),
              'total': len(folders), 'published': 0, 'skipped': len(folders) - len(missing),
              'failed': 0, 'results': []}
    if not missing:
        return report
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / 'auto-cover.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another automatic cover sweep is running') from None
        batch_dir = state_dir / 'frame-covers' / 'batches'
        batch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal = batch_dir / (uuid.uuid4().hex + '.json')
        report['report_path'] = str(journal)

        def save():
            atomic_write(journal, (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode())
            journal.chmod(0o600)

        report['status'] = 'running'
        save()
        for folder in missing:
            try:
                result = ensure_cover(folder.name, root, output, state_dir, time=time)
                status = result['status']
                report['published' if status == 'published' else 'skipped'] += 1
                record = {k: result[k] for k in ('folder', 'status', 'cover_source', 'scraping', 'preview_id', 'seconds', 'image') if k in result}
            except Exception as error:
                report['failed'] += 1
                record = {'folder': folder.name, 'status': 'failed', 'error_type': type(error).__name__}
            report['results'].append(record)
            save()
            if progress:
                progress(record)
        report['status'] = 'auto_cover_complete'
        save()
    return report
