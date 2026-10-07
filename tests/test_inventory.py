import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import inventory


class InventoryTests(unittest.TestCase):
    def probe(self, *args, **kwargs):
        return SimpleNamespace(stdout=json.dumps({'streams': [{'codec_type': 'video', 'codec_name': 'h264'}],
                                                 'format': {'duration': '10'}}))

    def test_inventory_is_stable_and_duplicate_codes_get_unique_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'TEST-001.mp4').write_bytes(b'first synthetic file')
            (root / 'TEST_001.mkv').write_bytes(b'second synthetic file')
            (root / 'notes.txt').write_text('ignored')
            with patch.object(inventory.subprocess, 'run', side_effect=self.probe):
                one = inventory.make_inventory(root)
                self.assertEqual(one, inventory.make_inventory(root))
            self.assertEqual(len(one['items']), 2)
            self.assertEqual(len({i['folder'] for i in one['items']}), 2)
            self.assertTrue(all(i['code'] == 'TEST-001' for i in one['items']))
            self.assertEqual((root / 'TEST-001.mp4').read_bytes(), b'first synthetic file')

    def test_symlink_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'original.mp4').write_bytes(b'original')
            (root / 'link.mp4').symlink_to(root / 'original.mp4')
            with patch.object(inventory.subprocess, 'run', side_effect=self.probe), self.assertRaises(ValueError):
                inventory.make_inventory(root)
