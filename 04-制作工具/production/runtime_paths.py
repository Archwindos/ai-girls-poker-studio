"""Portable paths and fingerprints for the local creation tool."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
TOOLS = PROJECT / '04-制作工具'
LIBRARY = PROJECT / '02-创作资产'
STATE = PROJECT / '05-制作记录/当前状态'
EXPORTS = PROJECT / '01-发布成品/导出批次'
RESOURCES = TOOLS / 'resources'


def project_path(value, *, project=PROJECT, within=None):
    path = Path(value)
    if not path.is_absolute():
        path = project / path
    path = path.resolve()
    boundary = (within or project).resolve()
    if not path.is_relative_to(boundary):
        raise ValueError('文件路径必须位于允许的项目目录内。')
    return path


def code_fingerprint():
    digest = hashlib.sha256()
    files = list((TOOLS / 'production').glob('*.py'))
    files += [TOOLS / 'webui/serve_review.py']
    for file in sorted(files):
        digest.update(file.name.encode('utf-8'))
        digest.update(file.read_bytes())
    return digest.hexdigest()[:20]


def pid_alive(identifier):
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, int(identifier))
            if not handle:
                return False
            try:
                result = wintypes.DWORD()
                return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(result))) and result.value == 259
            finally:
                kernel.CloseHandle(handle)
        os.kill(int(identifier), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False
