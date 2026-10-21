"""Isolated HTTP checks; production selections and exports are never written."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from runtime_paths import PROJECT, project_path
from runtime_state import RuntimeOwner, atomic_json

sys.path.insert(0, str(PROJECT / '04-制作工具/webui'))
from serve_review import create_server


class RuntimeTests(unittest.TestCase):
    def test_isolated_http_and_state(self):
        legacy = PROJECT / '04-制作工具/webui'
        original = {name: (legacy / name).read_bytes() for name in ('selections.json', 'deck-config.json')}
        scratch = PROJECT / '04-制作工具/测试缓存'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='runtime-isolated-', dir=scratch) as directory:
            root = Path(directory).resolve()
            self.assertTrue(root.is_relative_to(scratch))
            library = root / 'library'
            library.mkdir()
            image = library / 'test.png'
            Image.new('RGB', (100, 150), '#879f9d').save(image)
            config = json.loads(original['deck-config.json'])
            selection = json.loads(original['selections.json'])
            assets, groups = {}, []
            layout = {'top_index_box': [.08, .08, .2, .23], 'bottom_index_box': [.8, .77, .92, .92],
                      'name_boxes': [{'box': [.3, .05, .7, .09], 'rotation_deg': 0}]}
            relative = image.relative_to(PROJECT).as_posix()
            for role, slots in config['slots'].items():
                items = []
                for identifier in slots.values():
                    assets[identifier] = {'id': identifier, 'role': role, 'kind': 'face', 'art_source': relative,
                                          'preview': relative, 'layout': layout, 'production_ready': True}
                    items.append({'id': identifier, 'model': role, 'name': role, 'file': relative})
                groups.append({'slug': role, 'name': role, 'rank': '', 'items': items})
            assets[config['back']] = {'id': config['back'], 'kind': 'back', 'art_source': relative, 'preview': relative}
            for face, identifier in config['box'].items():
                assets[identifier] = {'id': identifier, 'kind': 'box', 'face': face, 'art_source': relative, 'preview': relative}
            atomic_json(library / 'asset-registry.json', {'schema_version': 1, 'revision': 1, 'assets': assets,
                                                        'name_assets': {}, 'gallery': {'groups': groups}})
            interrupted = root / 'exports/20260101T000000-0000000000'
            interrupted.mkdir(parents=True)
            atomic_json(interrupted / 'job.json', {'id': interrupted.name, 'status': 'queued', 'worker_pid': 999999999})
            server = create_server(port=0, state_root=root / 'state', exports_root=root / 'exports',
                                   library_root=library, publication_root=root / 'published', start_worker=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = 'http://127.0.0.1:' + str(server.server_port)

            def request(path, payload=None, method=None, fingerprint=True):
                headers = {'Content-Type': 'application/json'}
                if fingerprint:
                    headers['X-Deck-Code-Version'] = server.owner.fingerprint
                data = None if payload is None else json.dumps(payload).encode()
                req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
                try:
                    response = urllib.request.urlopen(req, timeout=60)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    raw = response.read()
                    return response.status, json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw

            try:
                self.assertEqual(request('/api/health')[1]['api_version'], 3)
                self.assertEqual(json.loads((interrupted / 'job.json').read_text(encoding='utf8'))['status'], 'failed')
                self.assertTrue(request('/api/health')[1]['active_writer'])
                with self.assertRaises(ValueError):
                    RuntimeOwner(root / 'state', server.server_port)
                self.assertEqual(len(request('/api/selections')[1]['selected_ids']), 28)
                self.assertEqual(request('/api/selections')[1]['selected_export']['count'], 28)
                for name, content in original.items():
                    self.assertEqual((root / 'state' / name).read_bytes(), content)
                first = selection['selected_ids'][0]
                self.assertEqual(request('/api/library/' + first + '/art')[0], 200)
                self.assertTrue(request('/catalog.json')[1]['groups'][0]['items'][0]['file'].startswith('api/library/'))
                self.assertEqual(request('/api/selections', {'base_revision': 7, 'selected_ids': []}, fingerprint=False)[0], 409)
                status, data = request('/api/selections', {'base_revision': 7, 'selected_ids': selection['selected_ids']})
                self.assertEqual((status, data['revision']), (200, 7))
                self.assertEqual((root / 'state/selections.json').read_bytes(), original['selections.json'])
                payload = {'id': 'NEW-TEST', 'role': 'llama', 'display_name': 'new',
                           'image_base64': base64.b64encode(image.read_bytes()).decode()}
                status, imported = request('/api/library/import', payload)
                self.assertEqual(status, 201)
                self.assertFalse(imported['asset']['production_ready'])
                imported_gallery = next(item for group in request('/catalog.json')[1]['groups'] for item in group['items'] if item['id'] == 'NEW-TEST')
                self.assertNotIn('production_asset_id', imported_gallery)
                self.assertNotIn('NEW-TEST', request('/api/selections')[1]['selected_ids'])
                self.assertEqual(request('/api/library/import', payload)[0], 409)
                back_payload = dict(payload, id='NEW-BACK', kind='back', has_bleed=True)
                status, back = request('/api/library/import', back_payload)
                self.assertEqual(status, 201)
                self.assertEqual(back['asset']['source_bleed_mm'], 3)
                self.assertEqual(back['asset']['back_safety']['method'], 'edge-background')
                box_payload = dict(payload, id='NEW-BOX', kind='box', face='front')
                self.assertEqual(request('/api/library/import', box_payload)[0], 201)
                self.assertEqual(request('/api/library/import', dict(box_payload, id='BAD-BOX', face='../outside'))[0], 400)
                current = request('/api/library/NEW-TEST/layout')[1]
                self.assertEqual(request('/api/library/NEW-TEST/layout', {'base_revision': current['library_revision'], 'layout': layout}, 'PUT')[0], 200)
                self.assertEqual(request('/api/library/NEW-TEST/layout', {'base_revision': current['library_revision'], 'layout': layout}, 'PUT')[0], 409)
                self.assertEqual(request('/api/selections', {'base_revision': 7, 'selected_ids': []})[1]['revision'], 8)
                restored = request('/api/restore', {'base_revision': 8, 'revision': 7})[1]
                self.assertEqual((restored['revision'], len(restored['selected_ids'])), (9, 28))
                self.assertEqual(restored['selected_export']['status'], 'complete')
                self.assertEqual(request('/api/selections/export', {})[0], 200)
                self.assertEqual(request('/api/selections', {'base_revision': 7, 'selected_ids': []})[0], 409)
                extended = request('/api/selections', {'base_revision': 9, 'selected_ids': selection['selected_ids'] + ['NEW-TEST']})[1]
                self.assertEqual(extended['revision'], 10)
                status, job = request('/api/exports', {'config_version': 7, 'selection_revision': 10})
                self.assertEqual(status, 202)
                frozen = json.loads((root / 'exports' / job['id'] / 'snapshot/snapshot.json').read_text(encoding='utf8'))
                self.assertEqual(len(frozen['assets']), 35)
                self.assertEqual(len(frozen['selected_assets']), 29)
                self.assertIn('NEW-TEST', frozen['selected_assets'])
                self.assertTrue(all(not Path(key).is_absolute() for key in frozen['sources']))
                with self.assertRaises(ValueError):
                    project_path(PROJECT.parent / 'outside.png')
                # Simulate a newer code version claiming the same state while the old server remains alive.
                import runtime_state
                real_fingerprint = runtime_state.code_fingerprint
                try:
                    runtime_state.code_fingerprint = lambda: 'new-isolated-version'
                    RuntimeOwner(root / 'state', server.server_port)
                finally:
                    runtime_state.code_fingerprint = real_fingerprint
                self.assertFalse(request('/api/health')[1]['active_writer'])
                self.assertEqual(request('/api/selections', {'base_revision': 10, 'selected_ids': []})[0], 409)
                self.assertEqual(len(list((root / 'exports').iterdir())), 2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        for name, content in original.items():
            self.assertEqual((legacy / name).read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
