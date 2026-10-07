import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import import_metadata


class BundledProviderTests(unittest.TestCase):
    def providers(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor/jav-metadata-syncer/backend'))
        from app import clients
        return clients

    def movie(self, date='2026-10-07'):
        return SimpleNamespace(code='TEST-001', release_date=date, runtime_minutes=120,
                               studio='Example', director='Example',
                               actresses=[SimpleNamespace(name='Example')],
                               cover_url='https://images.example/cover.jpg',
                               source_url='https://source.example/TEST-001')

    def test_bundled_provider_import_and_queries_are_connected(self):
        clients = self.providers()
        self.assertEqual(set(clients.list_providers()), {'javbus', 'javtrailers', 'missav'})
        candidates = []
        with patch.object(clients, 'get_provider', side_effect=lambda name:
                          SimpleNamespace(search=AsyncMock(return_value=self.movie()))) as get:
            facts, sources, failures = asyncio.run(import_metadata.fetch_facts('TEST-001', candidates))
        self.assertEqual([call.args[0] for call in get.call_args_list], ['javbus', 'javtrailers'])
        self.assertEqual(sources, ['javbus', 'javtrailers'])
        self.assertEqual(facts['code'], 'TEST-001')
        self.assertEqual(len(candidates), 2)
        self.assertEqual(failures, [])

    def test_conflicting_successful_sources_do_not_publish(self):
        clients = self.providers()
        def provider(name):
            return SimpleNamespace(search=AsyncMock(return_value=self.movie(
                '2026-10-07' if name == 'javbus' else '2026-10-08')))
        with patch.object(clients, 'get_provider', side_effect=provider), \
             self.assertRaisesRegex(ValueError, 'Conflicting premiered'):
            asyncio.run(import_metadata.fetch_facts('TEST-001'))
