"""New video imports always check artwork after scraping, including failures."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import batch_media as batch


class CoverImportTests(unittest.TestCase):
    def test_unrecognised_code_still_checks_cover(self):
        with patch.object(batch, 'run_logged') as run:
            result = batch.enhance_catalogue({'folder': 'VIDEO_001'}, Path('/job'))
        self.assertEqual(result, ('no_recognized_code', 'checked'))
        self.assertEqual(run.call_count, 1)
        self.assertIn('--auto-cover', run.call_args.args[0][-1])
        self.assertNotIn('--scrape-attempted', run.call_args.args[0][-1])

    def test_failed_scraper_still_checks_cover(self):
        with patch.object(batch, 'run_logged', side_effect=[RuntimeError('source unavailable'), None]) as run:
            result = batch.enhance_catalogue({'folder': 'TEST_001', 'code': 'TEST-001'}, Path('/job'))
        self.assertEqual(result, ('unavailable', 'checked'))
        self.assertEqual(run.call_count, 2)
        self.assertIn('--apply', run.call_args_list[0].args[0][-1])
        self.assertIn('--auto-cover', run.call_args_list[1].args[0][-1])
        self.assertIn('--scrape-attempted', run.call_args_list[1].args[0][-1])

    def test_cover_failure_does_not_break_verified_video_import(self):
        with patch.object(batch, 'run_logged', side_effect=RuntimeError('unavailable')):
            result = batch.enhance_catalogue({'folder': 'VIDEO_001'}, Path('/job'))
        self.assertEqual(result, ('no_recognized_code', 'unavailable'))
