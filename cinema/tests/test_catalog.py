import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('catalog', Path(__file__).parents[1] / 'scripts/build_catalog.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'private'
        self.video = self.root / '测试 01'
        self.video.mkdir(parents=True)
        self.out = Path(self.tmp.name) / 'public'
        (self.video / 'index.m3u8').write_text('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key?sign=PRIVATE"\n#EXTINF:6.2,\nsecret.ts\n#EXTINF:5.8,\nsecret.ts\n')
        (self.video / 'key').write_bytes(b'private key')

    def test_exports_metadata_not_secrets(self):
        (self.video / 'movie.nfo').write_text('<movie><title>影片名</title><plot>&lt;b&gt;简介&lt;/b&gt;</plot><genre>纪录片</genre><tag>收藏</tag><actor><name>甲</name></actor><runtime>999</runtime></movie>')
        (self.video / 'poster.jpg').write_bytes(b'example raster')
        result = module.build_catalog(self.root, self.out)['videos'][0]
        self.assertEqual(result['title'], '影片名')
        self.assertEqual(result['description'], '简介')
        self.assertEqual(result['duration'], 12)
        self.assertEqual(result['actors'], ['甲'])
        self.assertEqual(result['tags'], ['纪录片', '收藏'])
        text = (self.out / 'catalog.json').read_text()
        self.assertNotIn('PRIVATE', text)
        self.assertFalse((self.out / 'key').exists())
        self.assertTrue((self.out / result['cover']).exists())

    def test_missing_metadata_is_valid(self):
        video = module.build_catalog(self.root, self.out)['videos'][0]
        self.assertEqual(video['title'], '测试 01')
        self.assertEqual(video['cover'], '')

    def test_discovers_same_folder_subtitles_without_duplicate_formats(self):
        for name in ['影片.zh-CN.srt', '影片.zh-CN.vtt', 'index.en.srt']:
            (self.video / name).write_text('字幕测试')
        (self.video / 'outside.ja.srt').symlink_to(self.video / 'key')
        tracks = module.build_catalog(self.root, self.out)['videos'][0]['subtitles']
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0]['path'], '/m3u8/测试 01/影片.zh-CN.vtt')
        self.assertEqual(tracks[0]['language'], 'zho')
        self.assertTrue(tracks[0]['default'])
        self.assertFalse(tracks[1]['default'])
        self.assertFalse((self.out / '影片.zh-CN.vtt').exists())

    def test_symlinks_not_exported(self):
        (self.video / 'poster.jpg').symlink_to(self.video / 'key')
        (self.root / 'another').symlink_to(self.video)
        result = module.build_catalog(self.root, self.out)
        self.assertEqual(len(result['videos']), 1)
        self.assertEqual(result['videos'][0]['cover'], '')

    def test_malformed_nfo_preserves_previous_catalog(self):
        module.build_catalog(self.root, self.out)
        before = (self.out / 'catalog.json').read_bytes()
        (self.video / 'movie.nfo').write_text('<movie>bad')
        with self.assertRaises(module.ET.ParseError):
            module.build_catalog(self.root, self.out)
        self.assertEqual((self.out / 'catalog.json').read_bytes(), before)

    def test_private_output_rejected(self):
        with self.assertRaises(ValueError):
            module.build_catalog(self.root, self.root / 'public')

    def test_entity_nfo_rejected(self):
        (self.video / 'movie.nfo').write_text('<!DOCTYPE movie [<!ENTITY x SYSTEM "file:///etc/passwd">]><movie><title>&x;</title></movie>')
        with self.assertRaises(ValueError):
            module.build_catalog(self.root, self.out)
