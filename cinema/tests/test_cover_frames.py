import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, unquote, urlsplit

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import cover_frames as f
import import_metadata as metadata
from metadata_artwork import image_payload
from build_catalog import atomic_write


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'media'
        self.folder = self.root / 'SAMPLE1_001_中文'
        self.folder.mkdir(parents=True)
        self.state = self.base / 'state'
        self.output = self.base / 'public'
        self.source = ('#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-TARGETDURATION:6\n'
                       '#EXT-X-MEDIA-SEQUENCE:0\n'
                       '#EXT-X-KEY:METHOD=AES-128,URI="/d/m3u8/SAMPLE1_001_%E4%B8%AD%E6%96%87/key?sign=old",IV=0x00000000000000000000000000000000\n'
                       '#EXTINF:6,\n#EXT-X-BYTERANGE:188@0\n'
                       '/p/cloud/raw/SAMPLE1_001_%E4%B8%AD%E6%96%87/pack_000.ts\n#EXT-X-ENDLIST\n')
        (self.folder / 'index.m3u8').write_text(self.source)
        (self.folder / 'key').write_bytes(b'0123456789abcdef')
        (self.folder / 'movie.nfo').write_text('<movie><title>Manual title</title></movie>')

    def make_preview(self):
        ident = 'a' * 32
        run = self.state / 'frame-covers' / ident
        run.mkdir(parents=True)
        image = io.BytesIO()
        Image.new('RGB', (960, 540), '#609050').save(image, 'JPEG')
        raw = image.getvalue()
        (run / 'poster.jpg').write_bytes(raw)
        report = {'status': 'preview', 'preview_id': ident, 'folder': self.folder.name,
                  'seconds': 3, 'image': f.inspect_image(raw),
                  'playlist_sha256': f.playlist_source(self.folder)[2]}
        (run / 'report.json').write_text(json.dumps(report))
        return ident, run, raw

    def test_time_and_media_boundaries(self):
        self.assertEqual(f.seconds('01:02:03.5'), 3723.5)
        self.assertEqual(f.seconds('05:30'), 330)
        self.assertEqual(f.seconds('3.5'), 3.5)
        for time in ('nan', 'inf', '-1', '00:60:00', '1.5:02', '1:2:3:4'):
            with self.assertRaises(ValueError):
                f.seconds(time)
        with self.assertRaises(ValueError):
            f.preview_frame(self.folder.name, '6', self.root, self.state)
        for name in ('../outside', '.', '/absolute', 'bad\nname'):
            with self.assertRaises(ValueError):
                f.media_folder(self.root, name)

    def test_refresh_preserves_crypto_and_ranges_without_old_signatures(self):
        class Api:
            def signed(self, path, prefix):
                return 'http://127.0.0.1:5244/' + prefix + quote(path) + '?sign=fresh'
        text = f.refresh_playlist(self.source, self.folder.name, Api())
        self.assertNotIn('sign=old', text)
        self.assertIn('IV=0x00000000000000000000000000000000', text)
        self.assertIn('#EXT-X-BYTERANGE:188@0', text)
        self.assertIn('?sign=fresh', text)
        for malicious in ('http://evil.example/media.ts', '/p/cloud/raw/other/pack_000.ts',
                          '/p/cloud/raw/SAMPLE1_001_%E4%B8%AD%E6%96%87/../key'):
            with self.assertRaises(ValueError):
                f.refresh_playlist(self.source.replace('/p/cloud/raw/SAMPLE1_001_%E4%B8%AD%E6%96%87/pack_000.ts', malicious), self.folder.name, Api())

    def test_publish_exact_preview_and_preserve_private_source_files(self):
        ident, run, raw = self.make_preview()
        before = {name: (self.folder / name).read_bytes() for name in ('key', 'index.m3u8', 'movie.nfo')}
        result = f.publish_preview(ident, self.root, self.output, self.state)
        self.assertEqual(result['status'], 'published')
        self.assertEqual((self.folder / 'poster.jpg').read_bytes(), raw)
        video = json.loads((self.output / 'catalog.json').read_text())['videos'][0]
        self.assertEqual((self.output / video['cover']).read_bytes(), raw)
        for name, content in before.items():
            self.assertEqual((self.folder / name).read_bytes(), content)
        self.assertEqual(f.list_missing(self.root)['count'], 0)
        self.assertEqual(f.publish_preview(ident, self.root, self.output, self.state)['status'], 'preserved_existing')

    def test_configured_mount_refresh_retains_same_video_boundary(self):
        calls = []
        class Api:
            def signed(self, path, prefix):
                calls.append((prefix, path))
                return 'http://127.0.0.1:5244/' + prefix + quote(path) + '?sign=fresh'
        source = self.source.replace('/p/cloud/raw/', '/p/alternate/raw/')
        with patch.dict(os.environ, {'ALIST_SEGMENT_ROOT': '/alternate/raw'}):
            text = f.refresh_playlist(source, self.folder.name, Api())
        self.assertIn(('p', '/alternate/raw/' + self.folder.name + '/pack_000.ts'), calls)
        self.assertIn('#EXT-X-BYTERANGE:188@0', text)
        self.assertIn('IV=0x00000000000000000000000000000000', text)
        for invalid in ('other', self.folder.name + '/../other'):
            bad = source.replace('/p/alternate/raw/SAMPLE1_001_%E4%B8%AD%E6%96%87/', '/p/alternate/raw/' + quote(invalid) + '/')
            with patch.dict(os.environ, {'ALIST_SEGMENT_ROOT': '/alternate/raw'}), self.assertRaises(ValueError):
                f.refresh_playlist(bad, self.folder.name, Api())

    def test_existing_cover_is_never_replaced_or_decoded(self):
        ident, _, raw = self.make_preview()
        (self.folder / 'folder.png').write_bytes(b'old cover')
        self.assertEqual(f.publish_preview(ident, self.root, self.output, self.state)['status'], 'preserved_existing')
        with patch.object(f, 'Alist', side_effect=AssertionError('must not fetch')):
            self.assertEqual(f.preview_frame(self.folder.name, '3', self.root, self.state)['status'], 'preserved_existing')
        self.assertFalse((self.folder / 'poster.jpg').exists())
        self.assertEqual((self.folder / 'folder.png').read_bytes(), b'old cover')

    def test_auto_existing_cover_does_not_write_or_contact_alist(self):
        (self.folder / 'folder.png').write_bytes(b'original')
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.folder.iterdir()}
        with patch.object(f, 'preview_frame', side_effect=AssertionError('must not decode')), \
             patch.object(metadata, 'scrape_for_cover', side_effect=AssertionError('must not scrape')):
            self.assertEqual(f.ensure_cover(self.folder.name, self.root, self.output, self.state)['status'], 'preserved_existing')
            result = f.fill_missing(self.root, self.output, self.state)
        self.assertEqual(result['published'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.output.exists())
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.folder.iterdir()})

    def test_auto_missing_cover_defaults_to_second_three_and_publishes(self):
        ident, run, raw = self.make_preview()
        preview = json.loads((run / 'report.json').read_text())
        with patch.object(f, 'preview_frame', return_value=preview) as extract:
            result = f.ensure_cover(self.folder.name, self.root, self.output, self.state)
        self.assertEqual(extract.call_args.args[1], '00:00:03')
        self.assertEqual(result['status'], 'published')
        self.assertEqual((self.folder / 'poster.jpg').read_bytes(), raw)

    def test_auto_scraped_cover_prevents_any_ffmpeg_call(self):
        self.folder = self.folder.rename(self.root / 'TEST_001')
        facts = {'code': 'TEST-001', 'premiered': '2025-01-02', 'runtime': 120,
                 'studio': 'Example', 'director': 'Director', 'actors': ['Actor']}
        image = io.BytesIO(); Image.new('RGB', (128, 192), '#405040').save(image, 'JPEG')
        poster = image_payload(image.getvalue(), 'javbus')
        original_playlist = (self.folder / 'index.m3u8').read_bytes()
        with patch.object(metadata, 'fetch_facts', new=AsyncMock(return_value=(facts, ['javbus'], []))) as fetch, \
             patch.object(metadata, 'fetch_cover', new=AsyncMock(return_value=(poster, []))), \
             patch.object(f, 'preview_frame', side_effect=AssertionError('must not extract')):
            result = f.ensure_cover(self.folder.name, self.root, self.output, self.state)
        fetch.assert_awaited_once()
        self.assertEqual(result['cover_source'], 'scraper')
        self.assertEqual((self.folder / 'poster.jpg').read_bytes(), image.getvalue())
        self.assertEqual((self.folder / 'index.m3u8').read_bytes(), original_playlist)
        self.assertEqual(json.loads((self.output / 'catalog.json').read_text())['videos'][0]['title'], 'Manual title')

    def test_scraper_failure_precedes_frame_fallback(self):
        _, run, _ = self.make_preview()
        preview = json.loads((run / 'report.json').read_text())
        order = []

        def scrape(*args, **kwargs):
            order.append('scrape')
            return {'status': 'unavailable'}

        def frame(*args, **kwargs):
            order.append('frame')
            return preview

        with patch.object(metadata, 'scrape_for_cover', side_effect=scrape), \
             patch.object(f, 'preview_frame', side_effect=frame):
            result = f.ensure_cover(self.folder.name, self.root, self.output, self.state)
        self.assertEqual(order, ['scrape', 'frame'])
        self.assertEqual(result['cover_source'], 'frame')
        self.assertEqual(result['scraping']['status'], 'unavailable')
        self.assertEqual(result['seconds'], 3)

    def test_auto_failure_isolated_and_retry_skips_successful_films(self):
        ident, run, _ = self.make_preview()
        preview = json.loads((run / 'report.json').read_text())
        failed = self.root / 'FAIL_001'; failed.mkdir()
        (failed / 'index.m3u8').write_text(self.source)

        def extract(name, *args, **kwargs):
            if name == failed.name:
                raise RuntimeError('unavailable')
            return preview

        with patch.object(f, 'preview_frame', side_effect=extract), \
             patch.object(metadata, 'scrape_for_cover', return_value={'status': 'unavailable'}):
            result = f.fill_missing(self.root, self.output, self.state)
            retry = f.fill_missing(self.root, self.output, self.state)
        self.assertEqual((result['published'], result['failed']), (1, 1))
        self.assertEqual((retry['published'], retry['skipped'], retry['failed']), (0, 1, 1))
        self.assertFalse((failed / 'poster.jpg').exists())
        self.assertEqual(json.loads(Path(result['report_path']).read_text())['failed'], 1)

    def test_changed_preview_or_source_cannot_publish(self):
        ident, run, raw = self.make_preview()
        (run / 'poster.jpg').write_bytes(raw + b'changed')
        with self.assertRaises(ValueError):
            f.publish_preview(ident, self.root, self.output, self.state)
        (run / 'poster.jpg').write_bytes(raw)
        (self.folder / 'index.m3u8').write_text(self.source + '\n')
        with self.assertRaises(ValueError):
            f.publish_preview(ident, self.root, self.output, self.state)
        self.assertFalse((self.folder / 'poster.jpg').exists())

    def test_rollback_if_another_movie_has_invalid_nfo(self):
        ident, _, _ = self.make_preview()
        bad = self.root / 'OTHER'; bad.mkdir()
        (bad / 'index.m3u8').write_text('#EXTM3U\n')
        (bad / 'movie.nfo').write_text('broken xml')
        atomic_write(self.output / 'catalog.json', b'original catalog')
        with self.assertRaises(Exception):
            f.publish_preview(ident, self.root, self.output, self.state)
        self.assertFalse((self.folder / 'poster.jpg').exists())
        self.assertEqual((self.output / 'catalog.json').read_bytes(), b'original catalog')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg unavailable')
    def test_real_encrypted_byterange_hls_preview_and_late_seek(self):
        staging = self.base / 'staging'; staging.mkdir()
        key = staging / 'key'; key.write_bytes(b'0123456789abcdef')
        info = staging / 'key-info'
        info.write_text('key\n' + str(key) + '\n00000000000000000000000000000000\n')
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=10',
                        '-t', '12', '-c:v', 'libx264', '-threads', '2', '-g', '20', '-f', 'hls',
                        '-hls_time', '2', '-hls_key_info_file', str(info), '-hls_segment_filename',
                        str(staging / 'part_%03d.ts'), str(staging / 'index.m3u8')],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        pack = b''; lines = []
        for line in (staging / 'index.m3u8').read_text().splitlines():
            if line.startswith('#EXT-X-KEY:'):
                line = line.replace('URI="key"', 'URI="/d/m3u8/' + quote(self.folder.name) + '/key"')
            if line and not line.startswith('#'):
                data = (staging / line).read_bytes()
                lines += [f'#EXT-X-BYTERANGE:{len(data)}@{len(pack)}',
                          '/p/cloud/raw/' + quote(self.folder.name) + '/pack_000.ts']
                pack += data
            else:
                lines.append(line)
        (self.folder / 'index.m3u8').write_text('\n'.join(lines) + '\n')
        ranges = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = unquote(urlsplit(self.path).path)
                data = key.read_bytes() if path.endswith('/key') else pack
                header = self.headers.get('Range')
                if header:
                    start, end = header.removeprefix('bytes=').split('-')
                    start, end = int(start), int(end) if end else len(data) - 1
                    body = data[start:end + 1]
                    ranges.append((start, end))
                    self.send_response(206)
                    self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
                else:
                    body = data; self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Accept-Ranges', 'bytes'); self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)

        class Api:
            def signed(self, path, prefix):
                return f'http://127.0.0.1:{server.server_port}/{prefix}' + quote(path)

        before = {name: (self.folder / name).read_bytes() for name in ('key', 'movie.nfo', 'index.m3u8')}
        result = f.preview_frame(self.folder.name, '7', self.root, self.state, Api(), timeout=30)
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(result['image']['width'], 960)
        self.assertFalse((self.folder / 'poster.jpg').exists())
        self.assertFalse(self.output.exists())
        self.assertTrue(any(start > 0 for start, end in ranges))
        self.assertFalse(list((self.state / 'frame-covers').rglob('*.m3u8')))
        for name, content in before.items():
            self.assertEqual((self.folder / name).read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
