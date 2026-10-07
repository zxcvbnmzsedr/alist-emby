#!/usr/bin/env python3
"""Import basic catalogue facts using the bundled metadata providers.

Code, names, release date, runtime, studio and a cover are imported. Existing
title, description, tags and local artwork are preserved. No video is downloaded.
"""
import argparse
import asyncio
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

from build_catalog import atomic_write, build_catalog
from metadata_artwork import MAX_PAYLOAD_BYTES, decode_image, fetch_cover


def normalize_code(folder):
    if not folder or folder in ('.', '..') or '/' in folder or '\\' in folder:
        raise ValueError('Provide one media directory name, not a path')
    match = re.fullmatch(r'([A-Za-z]{2,10})[-_](\d{2,6})(?:[ _].*)?', folder)
    if not match:
        raise ValueError('Unsupported code format; expected e.g. TEST_001')
    return f'{match[1].upper()}-{match[2]}'


def basic_facts(movie, expected):
    actual = str(movie.code or '').strip().upper().replace('_', '-')
    if actual != expected:
        raise ValueError(f'Provider returned a different code: {actual}')
    released = str(movie.release_date or '').strip()
    if released:
        date.fromisoformat(released)
    duration = movie.runtime_minutes
    if duration is not None and (type(duration) is not int or duration <= 0):
        raise ValueError('Invalid runtime')
    def text(value):
        return str(value or '').strip()
    facts = {'code': actual, 'premiered': released, 'runtime': duration,
             'studio': text(movie.studio), 'director': text(movie.director),
             'actors': list(dict.fromkeys(text(a.name) for a in movie.actresses if text(a.name)))}
    if not released or not facts['actors'] or not facts['studio']:
        raise ValueError('Incomplete basic metadata; manual review required')
    return facts


async def fetch_facts(code, cover_candidates=None):
    source_root = Path(__file__).resolve().parents[1] / "vendor/jav-metadata-syncer"
    sys.path.insert(0, str(source_root / 'backend'))
    from app.clients import get_provider
    matches, failures = [], []
    for name in ('javbus', 'javtrailers'):
        try:
            movie = await asyncio.wait_for(get_provider(name).search(code), timeout=45)
            matches.append((name, basic_facts(movie, code)))
            if cover_candidates is not None and getattr(movie, 'cover_url', None):
                cover_candidates.append({'source': name, 'url': movie.cover_url,
                                         'referer': getattr(movie, 'source_url', None)})
        except Exception as error:
            # Never log response bodies, titles, image addresses or credentials.
            failures.append({'source': name, 'error_type': type(error).__name__})
    if not matches:
        raise RuntimeError('No verified metadata available: ' + json.dumps(failures))
    chosen = matches[0][1]
    for name, candidate in matches[1:]:
        for field in ('code', 'premiered', 'runtime'):
            if candidate[field] and chosen[field] and candidate[field] != chosen[field]:
                raise ValueError(f'Conflicting {field} from {name}; nothing written')
        if set(candidate['actors']) != set(chosen['actors']):
            raise ValueError(f'Conflicting cast from {name}; nothing written')
    return chosen, [name for name, _ in matches], failures


def merge_nfo(previous, facts):
    if previous:
        if len(previous) > 2_000_000 or b'<!DOCTYPE' in previous.upper() or b'<!ENTITY' in previous.upper():
            raise ValueError('Unsupported existing NFO')
        root = ET.fromstring(previous)
        if root.tag != 'movie':
            raise ValueError('Existing NFO is not a movie')
        identifier = root.findtext('num') or root.findtext("uniqueid[@type='num']")
        if identifier and identifier.strip().upper().replace('_', '-') != facts['code']:
            raise ValueError('Existing NFO has a different code')
    else:
        root = ET.Element('movie')
        ET.SubElement(root, 'title').text = facts['code']
    fields = {'num': facts['code'], 'year': facts['premiered'][:4],
              'premiered': facts['premiered'], 'releasedate': facts['premiered'],
              'runtime': facts['runtime'], 'studio': facts['studio'], 'director': facts['director']}
    for name, value in fields.items():
        if value is None or value == '':
            continue
        for old in list(root.findall(name)):
            root.remove(old)
        ET.SubElement(root, name).text = str(value)
    for old in list(root.findall("uniqueid[@type='num']")):
        root.remove(old)
    ET.SubElement(root, 'uniqueid', {'type': 'num', 'default': 'true'}).text = facts['code']
    # Keep actor artwork/roles already entered by the owner; add missing names.
    existing = {a.findtext('name', '').strip() for a in root.findall('actor')}
    for name in facts['actors']:
        if name not in existing:
            actor = ET.SubElement(root, 'actor')
            ET.SubElement(actor, 'name').text = name
    ET.indent(root, space='  ')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True) + b'\n'


def publish(folder, facts, media_root, output, state_dir, sources, failures, poster=None):
    target = media_root / folder
    if target.is_symlink() or not target.is_dir() or target.resolve().parent != media_root.resolve():
        raise ValueError('Invalid media directory')
    playlist = target / 'index.m3u8'
    if playlist.is_symlink() or not playlist.is_file():
        raise ValueError('Missing regular index.m3u8')
    destination = target / 'movie.nfo'
    if destination.is_symlink():
        raise ValueError('Refusing symbolic-link NFO')
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / 'import.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = destination.read_bytes() if destination.exists() else None
        new = merge_nfo(old, facts)
        cover_path, cover_bytes = None, None
        cover_report = {'status': 'not_requested'}
        if poster is not None:
            cover_bytes, details = decode_image(poster)
            existing_art = [target / f'{name}.{ext}'
                            for name in ('poster', 'folder', f'{folder}-poster', 'index-poster')
                            for ext in ('jpg', 'jpeg', 'png', 'webp', 'avif')]
            if any(p.is_symlink() for p in existing_art):
                raise ValueError('Refusing symbolic-link cover')
            if any(p.is_file() for p in existing_art):
                cover_report = {'status': 'preserved_existing'}
            else:
                cover_path = target / f'poster.{details["extension"]}'
                cover_report = {**details, 'source': poster['source'], 'status': 'installed', 'path': str(cover_path)}
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        run_dir = state_dir / 'imports' / f'{stamp}-{facts["code"]}'
        run_dir.mkdir(parents=True, mode=0o700)
        report = {'facts': facts, 'sources': sources, 'failed_sources': failures,
                  'cover': cover_report, 'status': 'prepared'}
        atomic_write(run_dir / 'candidate.nfo', new)
        if old is not None:
            atomic_write(run_dir / 'previous.nfo', old)
        if cover_path:
            atomic_write(run_dir / cover_path.name, cover_bytes)
        catalog_path = output / 'catalog.json'
        old_catalog = catalog_path.read_bytes() if catalog_path.exists() else None
        if old_catalog is not None:
            atomic_write(run_dir / 'previous-catalog.json', old_catalog)
        protected = [p for p in (playlist, target / 'key') if p.is_file()]
        before = {p.name: hashlib.sha256(p.read_bytes()).digest() for p in protected}
        try:
            atomic_write(destination, new)
            if cover_path:
                atomic_write(cover_path, cover_bytes)
            if any(hashlib.sha256(p.read_bytes()).digest() != before[p.name] for p in protected):
                raise RuntimeError('Playback files unexpectedly changed')
            catalog = build_catalog(media_root, output)
        except Exception:
            if old is None:
                destination.unlink(missing_ok=True)
            else:
                atomic_write(destination, old)
            if cover_path:
                cover_path.unlink(missing_ok=True)
            if old_catalog is not None:
                atomic_write(catalog_path, old_catalog)
            else:
                catalog_path.unlink(missing_ok=True)
            report['status'] = 'rolled_back'
            atomic_write(run_dir / 'report.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
            raise
        report.update(status='published', video_count=len(catalog['videos']), nfo=str(destination))
        atomic_write(run_dir / 'report.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
        return report


def scrape_for_cover(folder, media_root, output, state_dir):
    """Attempt the existing validated scraper before any automatic frame fallback."""
    try:
        code = normalize_code(folder)
    except ValueError:
        return {'status': 'unrecognized_code'}
    candidates = []
    try:
        facts, sources, failures = asyncio.run(fetch_facts(code, candidates))
        poster, cover_failures = asyncio.run(fetch_cover(candidates))
    except (RuntimeError, ValueError, ImportError, OSError) as error:
        return {'status': 'unavailable', 'error_type': type(error).__name__}
    failures.extend({'stage': 'cover', **failure} for failure in cover_failures)
    return publish(folder, facts, Path(media_root), Path(output), Path(state_dir), sources, failures, poster)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', nargs='?')
    parser.add_argument('--metadata-json', type=Path, help='Import a local verified facts JSON instead of fetching websites')
    parser.add_argument('--code', help='Explicit code for a media folder with a different name')
    parser.add_argument('--apply', action='store_true', help='Write NFO and refresh static catalogue; otherwise just inspect')
    parser.add_argument('--verified-stdin', action='store_true', help='Read basic facts fetched on another trusted machine over SSH')
    parser.add_argument('--skip-cover', action='store_true', help='Import text metadata only')
    frame_mode = parser.add_mutually_exclusive_group()
    frame_mode.add_argument('--frame', nargs='?', const='00:00:03', metavar='TIME', help='Preview an HLS frame (default second 3); --apply publishes it')
    frame_mode.add_argument('--auto-cover', action='store_true', help='Scrape missing covers first, then fall back to second 3; omit folder for all videos')
    frame_mode.add_argument('--list-missing-covers', action='store_true', help='List media folders without artwork')
    frame_mode.add_argument('--apply-preview', metavar='ID', help='Publish an existing frame preview by ID')
    parser.add_argument('--cover-time', default='00:00:03', metavar='TIME', help='Override automatic cover timestamp')
    parser.add_argument('--scrape-attempted', action='store_true', help='Internal: this single-video caller already attempted scraping')
    parser.add_argument('--media-root', type=Path, default=Path('/m3u8'))
    parser.add_argument('--output', type=Path, default=Path('catalog'))
    parser.add_argument('--state-dir', type=Path, default=Path('state/scraper'))
    args = parser.parse_args()
    if args.scrape_attempted and (not args.auto_cover or not args.folder):
        parser.error('--scrape-attempted requires a single-video --auto-cover call')
    if args.frame is not None or args.auto_cover or args.list_missing_covers or args.apply_preview:
        if args.verified_stdin or args.metadata_json or args.code or args.skip_cover:
            parser.error('Frame mode cannot be combined with metadata input options')
        from cover_frames import ensure_cover, fill_missing, list_missing, preview_frame, publish_preview
        if args.auto_cover:
            if args.folder:
                result = ensure_cover(args.folder, args.media_root, args.output, args.state_dir,
                    time=args.cover_time, scrape=not args.scrape_attempted)
            else:
                result = fill_missing(args.media_root, args.output, args.state_dir, time=args.cover_time,
                    progress=lambda record: print(f"Auto cover: {record['folder']} -> {record['status']}", file=sys.stderr, flush=True))
        elif args.list_missing_covers:
            if args.folder or args.apply:
                parser.error('--list-missing-covers does not take a folder or --apply')
            result = list_missing(args.media_root)
        elif args.apply_preview:
            if args.folder:
                parser.error('--apply-preview does not take a folder')
            result = publish_preview(args.apply_preview, args.media_root, args.output, args.state_dir)
        else:
            if not args.folder:
                parser.error('--frame requires a media folder')
            result = preview_frame(args.folder, args.frame, args.media_root, args.state_dir)
            if args.apply and result['status'] == 'preview':
                result = publish_preview(result['preview_id'], args.media_root, args.output, args.state_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get('failed', 0):
            sys.exit(1)
        return
    if not args.folder:
        parser.error('Provide a media folder or --list-missing-covers')
    from cover_frames import media_folder
    media_folder(args.media_root, args.folder)
    code = normalize_code(args.code or args.folder)
    if args.verified_stdin and args.metadata_json:
        parser.error('--verified-stdin and --metadata-json are mutually exclusive')
    if args.verified_stdin or args.metadata_json:
        if args.metadata_json:
            with args.metadata_json.open('rb') as stream:
                raw = stream.read(MAX_PAYLOAD_BYTES + 1)
        else:
            raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError('Metadata payload too large')
        payload = json.loads(raw)
        incoming = payload['facts']
        facts = basic_facts(SimpleNamespace(code=incoming['code'], release_date=incoming['premiered'],
            runtime_minutes=incoming['runtime'], studio=incoming['studio'], director=incoming['director'],
            actresses=[SimpleNamespace(name=name) for name in incoming['actors']]), code)
        sources = payload.get('sources', ['local'])
        if not sources or any(source not in ('javbus', 'javtrailers', 'local') for source in sources):
            raise ValueError('Unexpected source in verified metadata')
        failures = payload.get('failed_sources', [])
        poster = None if args.skip_cover else payload.get('poster')
    else:
        candidates = []
        facts, sources, failures = asyncio.run(fetch_facts(code, candidates))
        poster = None
        if not args.skip_cover:
            from cover_frames import cover_exists, media_folder
            if not cover_exists(media_folder(args.media_root, args.folder)):
                try:
                    poster, cover_failures = asyncio.run(fetch_cover(candidates))
                    failures.extend({'stage': 'cover', **failure} for failure in cover_failures)
                except RuntimeError as error:
                    failures.append({'stage': 'cover', 'error_type': type(error).__name__})
    if args.apply:
        result = publish(args.folder, facts, args.media_root, args.output, args.state_dir, sources, failures, poster)
        if not args.skip_cover:
            from cover_frames import ensure_cover
            try:
                result['cover_fallback'] = ensure_cover(args.folder, args.media_root, args.output, args.state_dir, scrape=False)
            except Exception as error:
                result['cover_fallback'] = {'status': 'failed', 'error_type': type(error).__name__}
    else:
        result = {'status': 'preview', 'facts': facts, 'sources': sources, 'failed_sources': failures}
        if poster is not None:
            decode_image(poster)
            result['cover'] = {key: value for key, value in poster.items() if key != 'data'}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        sys.exit(1)
