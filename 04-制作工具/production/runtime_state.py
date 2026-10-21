"""Single active writer across versioned local servers; legacy state stays separate."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import uuid
from pathlib import Path

from runtime_paths import code_fingerprint, pid_alive


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ValueError('另一页面正在保存，请稍后重试。') from error
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ValueError('另一页面正在保存，请稍后重试。') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class RuntimeOwner:
    def __init__(self, root, port, legacy_root=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'active-runtime.json'
        self.lock_path = self.root / '.writer.lock'
        self.fingerprint = code_fingerprint()
        self.token = uuid.uuid4().hex
        self.port = port
        with file_lock(self.lock_path):
            if self.path.is_file():
                previous = json.loads(self.path.read_text(encoding='utf-8'))
                if previous.get('code_fingerprint') == self.fingerprint and pid_alive(previous.get('pid')):
                    raise ValueError('相同版本已经运行，请打开 http://127.0.0.1:' + str(previous['port']))
            migrated = {}
            for name in ('selections.json', 'deck-config.json'):
                target = self.root / name
                if not target.exists() and legacy_root is not None:
                    source = Path(legacy_root) / name
                    if source.is_file():
                        source_bytes = source.read_bytes()
                        # Exclusive creation preserves any concurrent migration result.
                        with target.open('xb') as stream:
                            stream.write(source_bytes)
                        assert target.read_bytes() == source_bytes
                        migrated[name] = hashlib.sha256(target.read_bytes()).hexdigest()
            self.record = {'pid': os.getpid(), 'port': port, 'code_fingerprint': self.fingerprint,
                           'owner_token': self.token, 'state_root': str(self.root), 'migrated_sha256': migrated}
            atomic_json(self.path, self.record)

    def is_active(self):
        try:
            current = json.loads(self.path.read_text(encoding='utf-8'))
            return current.get('owner_token') == self.token
        except (OSError, ValueError):
            return False

    @contextlib.contextmanager
    def write(self, fingerprint):
        with file_lock(self.lock_path):
            if fingerprint != self.fingerprint or not self.is_active():
                raise PermissionError('此页面或服务版本已经退休。请重新双击启动选图，打开当前版本后保存。')
            yield
