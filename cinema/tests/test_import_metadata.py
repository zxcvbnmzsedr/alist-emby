import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import import_metadata as m
import tempfile
import io
import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from PIL import Image
from metadata_artwork import image_payload, decode_image, inspect_image, public_url


class ImportTests(unittest.TestCase):
    def poster(self):
        data = io.BytesIO()
        Image.new('RGB', (128, 192), '#405040').save(data, format='JPEG')
        return image_payload(data.getvalue(), 'javbus')

    def facts(self):
        return {'code':'TEST-001','premiered':'2025-01-02','runtime':120,'studio':'Example','director':'Director','actors':['Actor']}

    def test_normalize_and_reject_traversal(self):
        self.assertEqual(m.normalize_code('TEST_001'), 'TEST-001')
        with self.assertRaises(ValueError):m.normalize_code('../TEST_001')

    def test_code_must_match_source(self):
        with self.assertRaises(ValueError):m.basic_facts(SimpleNamespace(code='TEST-999'),'TEST-001')

    def test_merge_preserves_user_title_and_description(self):
        old=b'<movie><title>My title</title><plot>My notes</plot><actor><name>Actor</name><role>Role</role></actor></movie>'
        root=m.ET.fromstring(m.merge_nfo(old,self.facts()))
        self.assertEqual(root.findtext('title'),'My title')
        self.assertEqual(root.findtext('plot'),'My notes')
        self.assertEqual(root.findtext('actor/role'),'Role')
        self.assertEqual(len(root.findall('actor')),1)
        self.assertEqual(root.findtext('premiered'),'2025-01-02')

    def test_rollback_on_index_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); media=base/'media'; folder=media/'TEST_001';folder.mkdir(parents=True)
            (folder/'index.m3u8').write_text('#EXTM3U\n')
            (folder/'key').write_bytes(b'unchanged')
            original=b'<movie><title>Old</title></movie>'
            (folder/'movie.nfo').write_bytes(original)
            output=base/'public';output.mkdir()
            (output/'catalog.json').write_bytes(b'old catalogue')
            with patch.object(m,'build_catalog',side_effect=ValueError('bad other NFO')):
                with self.assertRaises(ValueError):m.publish('TEST_001',self.facts(),media,output,base/'state',['test'],[],self.poster())
            self.assertEqual((folder/'movie.nfo').read_bytes(),original)
            self.assertEqual((folder/'key').read_bytes(),b'unchanged')
            self.assertEqual(len(list((base/'state').glob('imports/*/previous.nfo'))),1)
            self.assertFalse((folder/'poster.jpg').exists())
            self.assertEqual((output/'catalog.json').read_bytes(), b'old catalogue')

    def test_publish_cover_and_preserve_existing_art(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);media=base/'media';folder=media/'TEST_001';folder.mkdir(parents=True)
            (folder/'index.m3u8').write_text('#EXTM3U\n#EXTINF:10,\npart.ts\n')
            (folder/'key').write_bytes(b'private')
            poster=self.poster()
            result=m.publish('TEST_001',self.facts(),media,base/'public',base/'state',['javbus'],[],poster)
            self.assertEqual(result['cover']['status'],'installed')
            raw,_=decode_image(poster)
            self.assertEqual((folder/'poster.jpg').read_bytes(),raw)
            catalogue=m.json.loads((base/'public/catalog.json').read_text())
            cover=catalogue['videos'][0]['cover']
            self.assertTrue(cover.startswith('covers/'))
            self.assertEqual((base/'public'/cover).read_bytes(),raw)
            self.assertFalse((base/'public/key').exists())
            result=m.publish('TEST_001',self.facts(),media,base/'public',base/'state',['javbus'],[],poster)
            self.assertEqual(result['cover']['status'],'preserved_existing')
            self.assertEqual((folder/'key').read_bytes(),b'private')

    def test_invalid_image_rejected_before_metadata_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);media=base/'media';folder=media/'TEST_001';folder.mkdir(parents=True)
            (folder/'index.m3u8').write_text('#EXTM3U\n')
            poster=self.poster();poster['sha256']='wrong'
            with self.assertRaises(ValueError):
                m.publish('TEST_001',self.facts(),media,base/'public',base/'state',['javbus'],[],poster)
            self.assertFalse((folder/'movie.nfo').exists())

    def test_scraper_cover_failure_still_publishes_metadata_and_calls_frame_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);media=base/'media';folder=media/'TEST_001';folder.mkdir(parents=True)
            (folder/'index.m3u8').write_text('#EXTM3U\n#EXTINF:10,\npart.ts\n')
            argv=['alist-emby-import','TEST_001','--apply','--media-root',str(media),
                  '--output',str(base/'public'),'--state-dir',str(base/'state')]
            with patch.object(sys,'argv',argv), contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(m,'fetch_facts',new=AsyncMock(return_value=(self.facts(),['javbus'],[]))), \
                 patch.object(m,'fetch_cover',new=AsyncMock(side_effect=RuntimeError('cover unavailable'))), \
                 patch('cover_frames.ensure_cover',return_value={'status':'published','seconds':3}) as fallback:
                m.main()
            fallback.assert_called_once()
            self.assertFalse(fallback.call_args.kwargs['scrape'])
            self.assertTrue((folder/'movie.nfo').is_file())
            self.assertTrue((base/'public/catalog.json').is_file())

    def test_existing_art_does_not_trigger_provider_cover_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);media=base/'media';folder=media/'TEST_001';folder.mkdir(parents=True)
            (folder/'index.m3u8').write_text('#EXTM3U\n#EXTINF:10,\npart.ts\n')
            raw,_=decode_image(self.poster());(folder/'poster.jpg').write_bytes(raw)
            mtime=(folder/'poster.jpg').stat().st_mtime_ns
            argv=['alist-emby-import','TEST_001','--apply','--media-root',str(media),
                  '--output',str(base/'public'),'--state-dir',str(base/'state')]
            with patch.object(sys,'argv',argv), contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(m,'fetch_facts',new=AsyncMock(return_value=(self.facts(),['javbus'],[]))), \
                 patch.object(m,'fetch_cover',new=AsyncMock(side_effect=AssertionError('must not download'))):
                m.main()
            self.assertEqual((folder/'poster.jpg').read_bytes(),raw)
            self.assertEqual((folder/'poster.jpg').stat().st_mtime_ns,mtime)

    def test_reject_html_and_truncated_image(self):
        with self.assertRaises(Exception):inspect_image(b'<html>403 Access denied</html>')
        raw,_=decode_image(self.poster())
        with self.assertRaises(Exception):inspect_image(raw[:len(raw)//2])

    def test_reject_private_and_non_https_cover_urls(self):
        for url in ('file:///etc/passwd', 'http://example.com/cover.jpg', 'https://user:pass@example.com/a'):
            with self.assertRaises(ValueError):public_url(url)
        with patch('metadata_artwork.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError):public_url('https://example.com/cover.jpg')
            with self.assertRaises(ValueError):public_url('https://www.javbus.com/cover.jpg')
        with patch('metadata_artwork.socket.getaddrinfo',return_value=[(2,1,6,'',('198.18.0.10',443))]):
            with self.assertRaises(ValueError):public_url('https://example.com/cover.jpg')
            self.assertEqual(public_url('https://www.javbus.com/cover.jpg'),'https://www.javbus.com/cover.jpg')
