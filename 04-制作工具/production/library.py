"""Unified project-local artwork registry and explicit candidate imports."""
from __future__ import annotations

import base64
import copy
import io
import json
import math
import re
import threading
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from runtime_paths import LIBRARY, PROJECT, project_path
from runtime_state import atomic_json, file_lock

MAX_IMAGE_BYTES = 25 * 1024 * 1024
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
ROLES = {'llama', 'hunyuan', 'minimax', 'mistral', 'ernie', 'qwen', 'glm', 'kimi',
         'grok', 'deepseek', 'gemini', 'gpt', 'claude', 'doubao'}


class LibraryConflict(ValueError):
    pass


def valid_box(value):
    return isinstance(value, list) and len(value) == 4 and all(type(v) in (int, float) and math.isfinite(v) for v in value) and 0 <= value[0] < value[2] <= 1 and 0 <= value[1] < value[3] <= 1


def checked_layout(value):
    if not isinstance(value, dict):
        raise ValueError('layout 必须是对象。')
    layout = copy.deepcopy(value)
    # Historical layout metadata named the original file; the registry owns paths now.
    layout.pop('asset', None)
    allowed = {'top_index_box', 'bottom_index_box', 'name_box', 'name_boxes', 'art_box',
               'name_color', 'background', 'coordinate_system'}
    if set(layout) - allowed:
        raise ValueError('layout 含不支持的字段。')
    if layout.get('coordinate_system', 'normalized_xyxy') != 'normalized_xyxy':
        raise ValueError('排版坐标须为 normalized_xyxy。')
    for key in ('top_index_box', 'bottom_index_box', 'name_box', 'art_box'):
        if key in layout and not valid_box(layout[key]):
            raise ValueError(key + ' 必须是0到1范围内的有效xyxy矩形。')
    if 'name_box' in layout and 'name_boxes' not in layout:
        layout['name_boxes'] = [{'box': layout['name_box'], 'rotation_deg': 0}]
    if 'name_boxes' in layout:
        if not isinstance(layout['name_boxes'], list) or not layout['name_boxes']:
            raise ValueError('name_boxes 必须包含至少一个名字框。')
        for entry in layout['name_boxes']:
            if not isinstance(entry, dict) or not valid_box(entry.get('box')) or entry.get('rotation_deg', 0) not in (0, 180):
                raise ValueError('名字框无效；rotation_deg 只支持0或180。')
    for key in ('name_color', 'background'):
        if key in layout and (not isinstance(layout[key], str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', layout[key])):
            raise ValueError(key + ' 必须是六位十六进制颜色。')
    layout['coordinate_system'] = 'normalized_xyxy'
    ready = all(key in layout for key in ('top_index_box', 'bottom_index_box', 'name_boxes'))
    return layout, ready


class Library:
    def __init__(self, root=LIBRARY):
        self.root = project_path(root)
        self.path = self.root / 'asset-registry.json'
        self.lock = threading.RLock()
        self._cached_signature = None
        self._cached_data = None

    def signature(self):
        stat = self.path.stat()
        return stat.st_mtime_ns, stat.st_size

    def invalidate(self):
        self._cached_signature = None
        self._cached_data = None

    def read(self):
        with self.lock:
            signature = self.signature()
            if signature != self._cached_signature:
                self._cached_data = self._read_validated()
                self._cached_signature = signature
            return copy.deepcopy(self._cached_data)

    def _read_validated(self):
        data = json.loads(self.path.read_text(encoding='utf-8-sig'))
        if data.get('schema_version') != 1 or not isinstance(data.get('assets'), dict):
            raise ValueError('不支持的创作资产库格式。')
        for item in data['assets'].values():
            for key in ('art_source', 'preview', 'name_asset'):
                if item.get(key):
                    project_path(item[key], within=self.root)
        for value in data.get('name_assets', {}).values():
            project_path(value, within=self.root)
        return data

    def catalog(self):
        data = self.read()
        catalog = copy.deepcopy(data.get('gallery', {'groups': []}))
        for group in catalog['groups']:
            for item in group['items']:
                item['file'] = 'api/library/' + quote(item['id'], safe='') + '/preview'
                item['artwork_file'] = 'api/library/' + quote(item['id'], safe='') + '/art'
                asset = data['assets'].get(item.get('asset_id', item['id']), data['assets'].get(item['id'], {}))
                item['production_ready'] = asset.get('production_ready', False)
        return catalog

    def asset(self, identifier):
        data = self.read()
        asset = data['assets'].get(identifier)
        if asset is None:
            raise FileNotFoundError('素材编号不存在。')
        return asset

    def media(self, identifier, field='preview'):
        asset = self.asset(identifier)
        value = asset.get(field) or asset.get('art_source')
        if not value:
            raise FileNotFoundError('该候选没有可用图像。')
        path = project_path(value, within=self.root)
        if not path.is_file():
            raise FileNotFoundError('素材文件不存在。')
        return path

    def layout(self, identifier):
        data = self.read()
        if identifier not in data['assets']:
            raise FileNotFoundError('素材编号不存在。')
        asset = data['assets'][identifier]
        return {'id': identifier, 'layout': asset.get('layout', {}),
                'production_ready': asset.get('production_ready', False), 'library_revision': data.get('revision', 1)}

    def update_layout(self, identifier, payload):
        layout, ready = checked_layout(payload.get('layout'))
        with self.lock, file_lock(self.root / '.library.lock'):
            data = self.read()
            if payload.get('base_revision') != data.get('revision', 1):
                raise LibraryConflict('素材库已更新，请重新读取后保存。')
            if identifier not in data['assets']:
                raise FileNotFoundError('素材编号不存在。')
            data['assets'][identifier].update(layout=layout, production_ready=ready, measured_layout=ready)
            data['revision'] = data.get('revision', 1) + 1
            atomic_json(self.path, data)
            self.invalidate()
        return self.layout(identifier)

    def import_candidate(self, payload, image_bytes=None):
        identifier = payload.get('id', '')
        kind = payload.get('kind', 'face')
        role = str(payload.get('role') or payload.get('model') or '').lower()
        name = payload.get('display_name') or role or identifier
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or kind not in {'face', 'back', 'box'} or (kind == 'face' and role not in ROLES):
            raise ValueError('需要有效的素材ID和已知角色role。')
        if kind == 'box' and payload.get('face') not in {'front', 'back', 'left', 'right', 'top', 'bottom'}:
            raise ValueError('牌盒导入需要有效的face。')
        if type(payload.get('has_bleed', False)) is not bool:
            raise ValueError('has_bleed 必须为布尔值。')
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError('display_name 长度须为1到100。')
        if image_bytes is None:
            try:
                image_bytes = base64.b64decode(payload.get('image_base64', ''), validate=True)
            except (ValueError, TypeError) as error:
                raise ValueError('image_base64 无效。') from error
        if not 0 < len(image_bytes) <= MAX_IMAGE_BYTES:
            raise ValueError('图像大小必须在1字节到25MiB之间。')
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.verify()
            extension = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}.get(image.format)
        if extension is None:
            raise ValueError('仅支持PNG、JPEG和WebP图像。')
        layout, ready = checked_layout(payload.get('layout', {}))
        group_slug = role if kind == 'face' else kind
        target = project_path(self.root / '导入素材' / group_slug / (identifier + extension), within=self.root)
        asset = {'id': identifier, 'kind': kind, 'display_name': name,
                 'art_source': target.relative_to(PROJECT).as_posix(), 'preview': target.relative_to(PROJECT).as_posix(),
                 'layout': layout, 'production_ready': ready if kind == 'face' else True, 'measured_layout': ready}
        if kind == 'face':
            asset['role'] = role
        else:
            asset['has_bleed'] = payload.get('has_bleed', False)
            if asset['has_bleed']:
                asset['source_bleed_mm'] = 3
        if kind == 'back':
            safety = payload.get('back_safety', {'method': 'edge-background'})
            if not isinstance(safety, dict) or safety.get('method') not in {'protected-box', 'edge-background'}:
                raise ValueError('牌背安全区需使用protected-box或edge-background。')
            if safety['method'] == 'protected-box' and not valid_box(safety.get('content_box')):
                raise ValueError('牌背content_box需要有效的归一化xyxy矩形。')
            asset['back_safety'] = copy.deepcopy(safety)
        if kind == 'box':
            asset['face'] = payload['face']
        with self.lock, file_lock(self.root / '.library.lock'):
            data = self.read()
            if identifier in data['assets'] or target.exists():
                raise LibraryConflict('素材ID或目标文件已存在，请使用新ID。')
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(image_bytes)
            try:
                data['assets'][identifier] = asset
                gallery = data.setdefault('gallery', {'groups': []})
                group = next((g for g in gallery['groups'] if g['slug'] == group_slug), None)
                if group is None:
                    group = {'slug': group_slug, 'name': name, 'rank': '', 'items': []}
                    gallery['groups'].append(group)
                gallery_item = {'id': identifier, 'asset_id': identifier, 'model': group_slug, 'name': name,
                                'file': asset['preview'], 'canonical_source': asset['art_source'], 'source_aliases': []}
                if kind in {'back', 'box'}:
                    gallery_item['production_asset_id'] = identifier
                group['items'].append(gallery_item)
                data['revision'] = data.get('revision', 1) + 1
                atomic_json(self.path, data)
                self.invalidate()
            except Exception:
                target.unlink(missing_ok=True)
                raise
        return {'asset': asset, 'library_revision': data['revision']}
