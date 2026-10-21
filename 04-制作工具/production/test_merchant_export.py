"""Frozen-input, dynamic-layout, and merchant-file acceptance checks."""
import copy
import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

import deck
import merchant_export as merchant


def snapshot_fixture(folder, alternative=False, dimensions=(63, 88)):
    source_dir = folder / 'snapshot' / 'sources'
    source_dir.mkdir(parents=True)
    source = source_dir / 'test-art.png'
    picture = Image.new('RGB', (360, 520), 'white')
    draw = ImageDraw.Draw(picture)
    draw.rectangle((25, 40, 325, 475), outline='#443322', width=7)
    draw.ellipse((100, 100, 260, 420), fill='#bd5739')
    picture.save(source)
    sources = {'fixture': {'frozen_path': source.relative_to(folder).as_posix(), 'bytes': source.stat().st_size, 'sha256': merchant.sha(source)}}
    if alternative:
        alternative_source = source_dir / 'candidate-art.png'
        draw.ellipse((100, 100, 260, 420), fill='#294db5')
        picture.save(alternative_source)
        sources['alternative'] = {'frozen_path': alternative_source.relative_to(folder).as_posix(),
            'bytes': alternative_source.stat().st_size, 'sha256': merchant.sha(alternative_source)}
    assets, slots = {}, {}
    layout = {'top_index_box': [.04, .04, .16, .23], 'bottom_index_box': [.84, .77, .96, .96],
              'name_boxes': [{'box': [.35, .03, .65, .08], 'rotation_deg': 0},
                             {'box': [.35, .92, .65, .97], 'rotation_deg': 180}]}
    for _, role, _ in deck.FAMILIES:
        asset_id = 'candidate-' + role
        assets[asset_id] = {'id': asset_id, 'role': role, 'kind': 'face', 'art_source': str(source),
                            'display_name': role, 'layout': copy.deepcopy(layout)}
        slots[role] = {'black': asset_id, 'red': asset_id}
    if alternative:
        assets['selected-candidate'] = {**assets['candidate-llama'], 'id': 'selected-candidate', 'art_source': str(alternative_source)}
        slots['llama']['black'] = 'selected-candidate'
    assets['back'] = {'id': 'back', 'kind': 'back', 'art_source': str(source)}
    boxes = {}
    for face in deck.BOX_FACES:
        key = 'box-' + face
        boxes[face] = key
        assets[key] = {'id': key, 'kind': 'box', 'art_source': str(source)}
    return {'config': {'version': 7, 'slots': slots, 'back': 'back', 'box': boxes, 'settings': {
        'card_width_mm': dimensions[0], 'card_height_mm': dimensions[1], 'bleed_mm': 3, 'dpi': 150,
        'safe_mm': 3, 'box_width_mm': 65, 'box_height_mm': 90, 'box_depth_mm': 18}},
        'assets': assets, 'sources': sources, 'selection': {'revision': 3}, 'frozen_at': 'fixture'}


class MerchantAcceptance(unittest.TestCase):
    def test_old_bleed_is_removed_before_new_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'bleed.png'
            picture = Image.new('RGB', (690, 940), '#ff0000')
            ImageDraw.Draw(picture).rectangle((30, 30, 659, 909), fill='#102030')
            picture.save(source)
            trim, report = merchant.trim_source({'id': 'back-v7', 'kind': 'back',
                'art_source': str(source), 'has_bleed': True}, (70, 96))
            self.assertEqual(trim.size, (630, 880))
            self.assertEqual(trim.getpixel((0, 0)), (16, 32, 48))
            self.assertEqual(report['source_bleed_mm'], 3)
            self.assertEqual(report['source_trim_mm'], [63, 88])

    def test_back_content_mask_blocks_a_cut(self):
        unsafe = Image.new('RGB', merchant.pixels((63, 88), 600), '#d0aa55')
        with self.assertRaisesRegex(ValueError, '牌背保护内容'):
            merchant.back_master(unsafe, {'id': 'back-v7'}, (63, 88), (67, 92), 300, merchant.print_profile({}))

    def test_preview_uses_formal_geometry_and_returns_upload_png(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = snapshot_fixture(Path(directory))
            settings = snapshot['config']['settings']
            settings['dpi'] = 300
            asset = snapshot['assets']['candidate-llama']
            data = merchant.preview_merchant_png(asset, 'A', 'SPADES', settings)
            with Image.open(BytesIO(data)) as image:
                self.assertEqual(image.mode, 'RGB')
                self.assertEqual(image.size, merchant.pixels((67, 92), 100))

    def test_frozen_source_tampering_is_blocked_before_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            snapshot = snapshot_fixture(folder)
            Path(next(iter(snapshot['assets'].values()))['art_source']).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, '校验失败'):
                merchant.build_merchant_export(folder, snapshot)
            self.assertFalse((folder / 'package').exists())

    def test_source_outside_frozen_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            snapshot = snapshot_fixture(folder)
            next(iter(snapshot['assets'].values()))['art_source'] = str(folder / 'live-candidate.png')
            with self.assertRaisesRegex(ValueError, '未记录'):
                merchant.build_merchant_export(folder, snapshot)

    def test_safety_minimum_cannot_be_disabled(self):
        with self.assertRaises(ValueError):
            merchant.print_profile({'print_profile': {'label_safe_mm': 0}})

    def test_complete_export_changed_candidate_and_changed_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hashes = []
            unchanged_hashes = []
            for n, (alternate, dimensions) in enumerate(((False, (63, 88)), (True, (63, 88)), (True, (70, 96)))):
                folder = root / str(n)
                snapshot = snapshot_fixture(folder, alternate, dimensions)
                original = copy.deepcopy(snapshot)
                result = merchant.build_merchant_export(folder, snapshot)
                self.assertEqual(snapshot, original, 'Export must not mutate the frozen caller config')
                report = json.loads((folder / 'export-report.json').read_text(encoding='utf-8'))
                self.assertEqual(report['config'], snapshot['config'])
                self.assertEqual(report['card_upload_mm'], [v + 4 for v in dimensions])
                self.assertEqual(report['config']['settings']['bleed_mm'], 3)
                self.assertEqual(report['print_profile']['trim_allowance_mm'], 2)
                self.assertEqual(len(report['files']), 61)
                self.assertEqual(len(list((folder / 'screen_png').glob('*.png'))), 61)
                self.assertEqual(sum(r['kind'] == 'front' for r in report['files']), 54)
                self.assertEqual(sum(r['kind'] == 'back' for r in report['files']), 1)
                self.assertEqual(sum(r['kind'] == 'box' for r in report['files']), 6)
                for plan in report['front_layout_checks']:
                    self.assertGreaterEqual(plan['nominal_label_min_edge_mm'], 5 - 1e-8)
                    self.assertEqual(len(plan['cut_checks']), 9)
                    self.assertTrue(all(row['protected_box_pixels_removed'] == 0 for row in plan['cut_checks']))
                with zipfile.ZipFile(result['archive']) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(len(archive.namelist()), 62)
                    self.assertEqual(sum(name.endswith('.jpg') for name in archive.namelist()), 61)
                    self.assertEqual(sum(name.endswith('.txt') for name in archive.namelist()), 1)
                    self.assertFalse(any(name.endswith('.pdf') or name.endswith('.png') for name in archive.namelist()))
                    for row in report['files']:
                        self.assertEqual(merchant.sha(folder / 'package' / row['file']), row['sha256'])
                        with Image.open(folder / 'package' / row['file']) as im:
                            self.assertEqual(im.mode, 'CMYK')
                            self.assertEqual(im.size, tuple(row['pixels']))
                            self.assertEqual(im.info['dpi'], (150, 150))
                            self.assertEqual(im.info['icc_profile'], merchant._profile_path().read_bytes())
                with zipfile.ZipFile(result['screen_archive']) as archive:
                    self.assertEqual(len(archive.namelist()), 61)
                    self.assertTrue(all(name.endswith('.png') for name in archive.namelist()))
                    self.assertIsNone(archive.testzip())
                self.assertEqual(len(report['back_checks']['cut_checks']), 9)
                self.assertTrue(all(row['protected_pixels_removed'] == 0 for row in report['back_checks']['cut_checks']))
                hashes.append(report['files'][0]['sha256'])
                unchanged_hashes.append(report['files'][1]['sha256'])
                self.assertFalse(list(folder.rglob('*.pdf')))
            self.assertNotEqual(hashes[0], hashes[1], 'A single changed selected candidate must change its rendered card')
            self.assertEqual(unchanged_hashes[0], unchanged_hashes[1], 'Other selected candidates must remain identical')
            self.assertNotEqual(hashes[1], hashes[2], 'New card dimensions must change rendered output')


if __name__ == '__main__':
    unittest.main()
