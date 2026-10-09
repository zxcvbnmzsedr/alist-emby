#!/usr/bin/env python3
"""Build a static catalogue from local HLS folders and Emby-style NFO sidecars.

Only descriptive fields and approved raster artwork leave the source directory.
Playlists, signing parameters and encryption keys are NEVER exported.
"""
import argparse
import hashlib
import html
import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from subtitle_sidecars import discover_subtitles


def regular_within(path, root):
    return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve())


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        f.write(data)
    temporary.chmod(0o644)
    temporary.replace(path)


def clean(value):
    return re.sub(r'<[^>]*>', '', html.unescape(value or '')).strip()


def nfo_fields(path):
    raw = path.read_bytes()
    if len(raw) > 2_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError(f'Unsupported NFO: {path.name}')
    root = ET.fromstring(raw)
    if root.tag != 'movie':
        raise ValueError(f'Expected <movie> in {path.name}')
    def value(tag):
        return clean(root.findtext(tag, ''))
    def values(tag):
        return list(dict.fromkeys(clean(e.text) for e in root.findall(tag) if clean(e.text)))
    return {
        'title': value('title'), 'originalTitle': value('originaltitle'),
        'description': value('plot') or value('outline'), 'year': value('year'),
        'tags': list(dict.fromkeys(values('genre') + values('tag'))),
        'actors': values('actor/name'), 'director': ' / '.join(values('director')),
        'studio': ' / '.join(values('studio')), 'rating': value('rating'),
        'premiered': value('premiered') or value('releasedate'),
    }


def artwork(folder, names, root, output):
    for name in names:
        for ext in ['jpg', 'jpeg', 'png', 'webp', 'avif']:
            path = folder / f'{name}.{ext}'
            if not regular_within(path, root):
                continue
            raw = path.read_bytes()
            if len(raw) > 20_000_000:
                raise ValueError(f'Artwork too large: {path.name}')
            digest = hashlib.sha256(raw).hexdigest()[:20]
            dest = f'covers/{digest}.{ext}'
            atomic_write(output / dest, raw)
            return dest
    return ''


def build_catalog(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not root.is_dir():
        raise ValueError(f'Media directory does not exist: {root}')
    if output == root or output.is_relative_to(root):
        raise ValueError('Output must be outside the private media directory')
    videos = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        playlist = folder / 'index.m3u8'
        if not regular_within(playlist, root):
            continue
        source = playlist.read_text(encoding='utf-8-sig')
        if not source.startswith('#EXTM3U'):
            raise ValueError(f'Invalid playlist: {folder.name}')
        duration = sum(float(v) for v in re.findall(r'^#EXTINF:([0-9.]+)', source, re.M))
        data = {}
        for name in ['movie.nfo', 'index.nfo', f'{folder.name}.nfo']:
            nfo = folder / name
            if regular_within(nfo, root):
                data = nfo_fields(nfo)
                break
        item = {'id': folder.name, 'title': data.get('title') or folder.name, 'description': '', 'tags': [], 'actors': [], **data}
        item['title'] = item['title'] or folder.name
        item.update(path=f'/m3u8/{folder.name}/index.m3u8', duration=round(duration, 2),
                    addedAt=datetime.fromtimestamp(playlist.stat().st_mtime, timezone.utc).isoformat(),
                    cover=artwork(folder, ['poster', 'folder', f'{folder.name}-poster', 'index-poster'], root, output),
                    backdrop=artwork(folder, ['fanart', 'backdrop', f'{folder.name}-fanart', 'index-fanart'], root, output))
        item['subtitles'] = discover_subtitles(folder, f'/m3u8/{folder.name}')
        videos.append(item)
    catalog = {'version': 1, 'updatedAt': datetime.now(timezone.utc).isoformat(), 'videos': videos}
    atomic_write(output / 'catalog.json', (json.dumps(catalog, ensure_ascii=False, indent=2) + '\n').encode())
    return catalog


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/m3u8')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        result = build_catalog(args.root, args.output)
        print(f"Catalogue updated: {len(result['videos'])} videos")
    except (ValueError, OSError, ET.ParseError) as error:
        print(f'Catalogue unchanged: {error}', file=sys.stderr)
        sys.exit(1)
