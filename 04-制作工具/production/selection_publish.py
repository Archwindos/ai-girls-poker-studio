"""Publish selected originals by revision without deleting or replacing user files."""
from __future__ import annotations

import hashlib
from pathlib import Path

from runtime_paths import PROJECT, LIBRARY, project_path
from runtime_state import atomic_json


def publish_selection(selection, registry, destination=None):
    destination = project_path(destination or PROJECT / '01-发布成品/当前选择')
    revision_root = destination / ('版本-' + str(selection['revision']))
    records = []
    for identifier in selection['selected_ids']:
        asset = registry['assets'].get(identifier)
        if asset is None:
            raise ValueError('选中素材缺少登记：' + identifier)
        source = project_path(asset['art_source'], within=Path(registry.get('library_root', LIBRARY)))
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        role = asset.get('role') or asset.get('kind', '其他')
        target = project_path(revision_root / '角色原画' / role / (identifier + source.suffix.lower()), within=revision_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('当前选择输出已有不同内容的同名文件，请保留原文件并使用新选图版本：' + identifier)
        else:
            with target.open('xb') as stream:
                stream.write(data)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError('选中原画复制校验失败：' + identifier)
        records.append({'id': identifier, 'role': role, 'source': source.relative_to(PROJECT).as_posix(),
                        'file': target.relative_to(PROJECT).as_posix(), 'bytes': len(data), 'sha256': digest})
    manifest = {'schema_version': 1, 'selection_revision': selection['revision'],
                'selected_ids': selection['selected_ids'], 'assets': records}
    revision_root.mkdir(parents=True, exist_ok=True)
    atomic_json(revision_root / 'selection-manifest.json', manifest)
    destination.mkdir(parents=True, exist_ok=True)
    atomic_json(destination / 'current.json', {'schema_version': 1, 'selection_revision': selection['revision'],
                                             'manifest': (revision_root / 'selection-manifest.json').relative_to(PROJECT).as_posix()})
    return {'status': 'complete', 'count': len(records), 'selection_revision': selection['revision'],
            'directory': revision_root.relative_to(PROJECT).as_posix()}
