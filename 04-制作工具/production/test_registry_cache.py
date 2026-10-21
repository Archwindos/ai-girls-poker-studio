"""Focused cache invalidation and live media-path checks in an isolated library."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import deck
import library
from runtime_paths import PROJECT
from runtime_state import atomic_json


class RegistryCacheTests(unittest.TestCase):
    def test_cache_and_invalidation(self):
        scratch = PROJECT / '04-制作工具/测试缓存'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='registry-cache-', dir=scratch) as directory:
            root = Path(directory).resolve()
            assert root.is_relative_to(scratch)
            picture = root / 'art.png'
            Image.new('RGB', (10, 15), 'gray').save(picture)
            relative = picture.relative_to(PROJECT).as_posix()
            data = {'schema_version': 1, 'revision': 1, 'assets': {'TEST-1': {
                'id': 'TEST-1', 'kind': 'face', 'role': 'llama', 'display_name': 'test',
                'art_source': relative, 'preview': relative, 'production_ready': False}},
                'name_assets': {}, 'gallery': {'groups': []}}
            atomic_json(root / 'asset-registry.json', data)
            store = library.Library(root)
            with patch.object(library, 'project_path', wraps=library.project_path) as resolver:
                first = store.read()
                self.assertGreater(resolver.call_count, 0)
                resolver.reset_mock()
                first['assets'].clear()
                self.assertIn('TEST-1', store.read()['assets'])
                self.assertEqual(resolver.call_count, 0)
                store.media('TEST-1', 'art_source')
                self.assertGreater(resolver.call_count, 0)
            registry = deck.Registry(external=root / 'asset-registry.json')
            with patch.object(registry, '_load_normalized', wraps=registry._load_normalized) as normalizer:
                loaded = registry.load()
                loaded['assets'].clear()
                self.assertIn('TEST-1', registry.load()['assets'])
                self.assertEqual(normalizer.call_count, 1)
                store.update_layout('TEST-1', {'base_revision': 1, 'layout': {}})
                self.assertEqual(store.read()['revision'], 2)
                self.assertEqual(registry.load()['library_revision'], 2)
                self.assertEqual(normalizer.call_count, 2)
            bad = copy.deepcopy(data)
            bad['assets']['TEST-1']['art_source'] = '../outside.png'
            atomic_json(root / 'asset-registry.json', bad)
            with self.assertRaises(ValueError):
                store.read()


if __name__ == '__main__':
    unittest.main()
