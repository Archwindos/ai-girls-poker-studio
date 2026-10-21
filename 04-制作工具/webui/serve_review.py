"""Local-only image selection UI with the adjacent production renderer."""

import argparse
import json
import os
import tempfile
import threading
import sys
import mimetypes
import hashlib
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
PRODUCTION = ROOT.parent / 'production'
sys.path.insert(0, str(PRODUCTION))
from deck import Production, ConfigConflict, ValidationError, FAMILIES, SUITS, BOX_FACES, preview_png, digest
from library import Library, LibraryConflict
from runtime_paths import PROJECT, LIBRARY, STATE, EXPORTS, project_path, code_fingerprint
from runtime_state import RuntimeOwner
from selection_publish import publish_selection
MAX_BODY = 64 * 1024


class RevisionConflict(ValueError):
    def __init__(self, current):
        super().__init__("选择已在其他页面更新，请刷新后重试。")
        self.current = current


class InvalidRequest(ValueError):
    pass


class SelectionStore:
    def __init__(self, root, catalog_provider=None):
        self.root = Path(root).resolve()
        self.path = self.root / "selections.json"
        self.lock = threading.RLock()
        self.catalog_provider = catalog_provider
        self.refresh_catalog()
        with self.lock:
            if not self.path.exists():
                self._write(self._initial_document([], None, "初始选择"))
            else:
                document = self._read()
                if "schema_version" not in document:
                    self._write(self._initial_document(document["selected_ids"], document.get("updated_at"), "升级前的选择"))

    def refresh_catalog(self):
        catalog = self.catalog_provider() if self.catalog_provider else json.loads((self.root / 'catalog.json').read_text(encoding='utf-8'))
        self.catalog_order = [item['id'] for group in catalog['groups'] for item in group['items']]
        self.valid_ids = set(self.catalog_order)

    @staticmethod
    def _initial_document(ids, updated_at, label):
        entry = {"revision": 1, "selected_ids": list(ids), "updated_at": updated_at, "label": label}
        return {"schema_version": 2, "revision": 1, "selected_ids": list(ids), "updated_at": updated_at, "history": [entry]}

    @staticmethod
    def _state(document):
        return {key: document[key] for key in ("selected_ids", "updated_at", "revision")}

    @staticmethod
    def _valid_selection(data):
        return (
            isinstance(data, dict)
            and isinstance(data.get("selected_ids"), list)
            and all(isinstance(item, str) for item in data["selected_ids"])
            and (data.get("updated_at") is None or isinstance(data["updated_at"], str))
        )

    def _read(self):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not self._valid_selection(data):
            raise ValueError("selections.json has an invalid selection format")
        if "schema_version" not in data:
            if "revision" in data or "history" in data:
                raise ValueError("selections.json has an incomplete history format")
            return data
        history = data.get("history")
        if data["schema_version"] != 2 or not isinstance(history, list) or not history:
            raise ValueError("selections.json has an invalid history format")
        for revision, entry in enumerate(history, 1):
            if (
                not self._valid_selection(entry)
                or type(entry.get("revision")) is not int
                or entry["revision"] != revision
                or not isinstance(entry.get("label"), str)
            ):
                raise ValueError("selections.json has an invalid history entry")
        if (
            type(data.get("revision")) is not int
            or data["revision"] != len(history)
            or data["selected_ids"] != history[-1]["selected_ids"]
            or data.get("updated_at") != history[-1].get("updated_at")
        ):
            raise ValueError("selections.json current selection does not match its history")
        return data

    def _write(self, document):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, prefix=".selections-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def current(self):
        with self.lock:
            return self._state(self._read())

    def history(self):
        with self.lock:
            document = self._read()
            return {"current_revision": document["revision"], "entries": list(reversed(document["history"]))}

    @staticmethod
    def _base_revision(payload):
        if not isinstance(payload, dict):
            raise InvalidRequest("客户端保存格式已过期，请刷新页面后重试。")
        base_revision = payload.get("base_revision")
        if type(base_revision) is not int or base_revision < 1:
            raise InvalidRequest("缺少有效的 base_revision，请刷新页面后重试。")
        return base_revision

    def update(self, payload, restore=False):
        base_revision = self._base_revision(payload)
        with self.lock:
            document = self._read()
            if base_revision != document["revision"]:
                raise RevisionConflict(self._state(document))
            if restore:
                revision = payload.get("revision")
                if type(revision) is not int or not 1 <= revision <= document["revision"]:
                    raise InvalidRequest("请选择有效的历史版本。")
                ids = document["history"][revision - 1]["selected_ids"]
                label = f"恢复版本 {revision}"
            else:
                ids = payload.get("selected_ids")
                label = "保存选择"
            if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
                raise InvalidRequest("selected_ids 必须是图片编号数组。")
            unknown = set(ids) - self.valid_ids
            if unknown:
                raise InvalidRequest("选择包含图册中已不存在的编号：" + ", ".join(sorted(unknown)))
            selected = set(ids)
            if selected == set(document["selected_ids"]):
                return self._state(document)
            ids = [item for item in self.catalog_order if item in selected]
            entry = {"revision": document["revision"] + 1, "selected_ids": ids, "updated_at": datetime.now(timezone.utc).isoformat(), "label": label}
            document.update(self._state(entry))
            document["history"].append(entry)
            self._write(document)
            return self._state(document)


class ReviewHandler(SimpleHTTPRequestHandler):
    server_version = 'AIReview/3.0'

    def __init__(self, request, client_address, server):
        super().__init__(request, client_address, server, directory=str(server.ui_root))

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob:; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        super().end_headers()

    def json_response(self, status, data):
        body = (json.dumps(data, ensure_ascii=False) + '\n').encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def local_request(self):
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host', '') not in hosts or self.headers.get('Origin') not in {None, *['http://' + host for host in hosts]} or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.json_response(403, {'error': 'Only localhost requests are allowed'})
            return False
        return True

    def file_response(self, path, content_type=None, download=None):
        path = Path(path)
        try:
            stream = path.open('rb')
        except OSError:
            self.json_response(404, {'error': '文件不存在。'})
            return
        with stream:
            self.send_response(200)
            self.send_header('Content-Type', content_type or mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(path.stat().st_size))
            if download:
                self.send_header('Content-Disposition', f'attachment; filename="{download}"')
            self.end_headers()
            if self.command != 'HEAD':
                import shutil
                shutil.copyfileobj(stream, self.wfile)

    def do_GET(self):
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        parts = [unquote(part) for part in path.strip('/').split('/')]
        try:
            if path == '/api/health':
                self.json_response(200, {'status': 'ok', 'app': 'ai-girls-deck', 'api_version': 3,
                    'code_fingerprint': self.server.owner.fingerprint, 'active_writer': self.server.owner.is_active(),
                    'ui_root': str(self.server.ui_root), 'state_root': str(self.server.store.root),
                    'images': len(self.server.store.valid_ids), 'local_only': True, 'history_enabled': True,
                    'selected_export': self.server.selected_export})
            elif path == '/catalog.json':
                self.json_response(200, self.server.library.catalog())
            elif path == '/api/library':
                self.json_response(200, self.server.library.read())
            elif len(parts) == 4 and parts[:2] == ['api', 'library']:
                identifier, action = parts[2:]
                if action == 'layout':
                    self.json_response(200, self.server.library.layout(identifier))
                elif action in {'preview', 'art'}:
                    self.file_response(self.server.library.media(identifier, 'art_source' if action == 'art' else 'preview'))
                else:
                    raise FileNotFoundError()
            elif path == '/api/selections':
                self.json_response(200, {**self.server.store.current(), 'selected_export': self.server.selected_export})
            elif path == '/api/history':
                self.json_response(200, self.server.store.history())
            elif path == '/api/deck-config':
                self.json_response(200, self.server.production.state())
            elif path.startswith('/api/exports/'):
                if len(parts) == 3:
                    self.json_response(200, self.server.production.job(parts[2]))
                elif len(parts) == 4 and parts[3] == 'download':
                    self.file_response(self.server.production.download(parts[2]), 'application/zip', 'ai-girls-deck.zip')
                elif len(parts) == 4 and parts[3] == 'screen-download':
                    folder = self.server.production.job_folder(parts[2])
                    job = self.server.production.job(parts[2])
                    if job['status'] != 'complete' or not job.get('screen_archive_relpath'):
                        raise FileNotFoundError()
                    self.file_response(project_path(folder / job['screen_archive_relpath'], within=folder), 'application/zip', 'ai-girls-screen.zip')
                else:
                    raise FileNotFoundError()
            elif path.startswith('/api/production/assets/'):
                identifier = parts[-1]
                self.file_response(self.server.library.media(identifier))
            elif path.startswith('/api/production/preview/'):
                self.production_preview(parts)
            elif self.allowed_static(path):
                super().do_GET()
            else:
                raise FileNotFoundError()
        except FileNotFoundError:
            self.json_response(404, {'error': '素材、接口或导出不存在。'})
        except (ValueError, KeyError, StopIteration) as error:
            self.json_response(400, {'error': str(error)})
        except OSError as error:
            self.json_response(500, {'error': str(error)})

    def production_preview(self, parts):
        if len(parts) != 5:
            raise ValueError('无效的预览。')
        role, suit = parts[3], parts[4].removesuffix('.png')
        rank = next(rank for rank, key, _ in FAMILIES if key == role)
        if suit not in [item[0] for item in SUITS] + ['BLACK_JOKER', 'RED_JOKER']:
            raise ValueError('无效花色。')
        color = 'red' if suit in ('HEARTS', 'DIAMONDS', 'RED_JOKER') else 'black'
        state = self.server.production.state()
        identifier = state['config']['slots'][role][color]
        if not identifier or identifier not in self.server.store.current()['selected_ids']:
            raise ValueError('请先勾选原画并指定此槽位。')
        asset = state['registry']['assets'][identifier]
        if not asset.get('production_ready'):
            raise ValueError('该候选尚未完成排版测量。')
        signature = json.dumps({'code': self.server.owner.fingerprint, 'asset': asset,
            'art_hash': digest(asset['art_source']), 'name_hash': digest(asset['name_asset']) if asset.get('name_asset') else None,
            'rank': rank, 'suit': suit, 'settings': state['config']['settings']}, sort_keys=True)
        cache = self.server.store.root / '.preview-cache'
        cache.mkdir(exist_ok=True)
        target = cache / (hashlib.sha256(signature.encode()).hexdigest() + '.png')
        if not target.is_file():
            target.write_bytes(preview_png(asset, rank, suit, state['config']['settings']))
        self.file_response(target, 'image/png')

    def allowed_static(self, path):
        return unquote(path) in {'/', '/index.html', '/app.js', '/style.css', '/production.js', '/production.css', '/library.js', '/library.css'}

    def do_HEAD(self):
        if not self.local_request():
            return
        if self.allowed_static(urlsplit(self.path).path):
            super().do_HEAD()
        else:
            self.json_response(404, {'error': 'Not found'})

    def payload(self, maximum=MAX_BODY):
        if self.headers.get_content_type() != 'application/json':
            raise InvalidRequest('请使用JSON请求。')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= maximum:
            raise InvalidRequest('请求大小超出限制。')
        result = json.loads(self.rfile.read(length).decode('utf-8'))
        if not isinstance(result, dict):
            raise InvalidRequest('请求必须为JSON对象。')
        return result

    def publish_selected(self):
        try:
            self.server.selected_export = publish_selection(self.server.store.current(), self.server.production.registry.load(), self.server.publication_root)
        except (OSError, ValueError) as error:
            self.server.selected_export = {'status': 'failed', 'error': str(error)}
        return self.server.selected_export

    def do_POST(self):
        self.write_request()

    def do_PUT(self):
        self.write_request()

    def write_request(self):
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        parts = [unquote(part) for part in path.strip('/').split('/')]
        try:
            with self.server.owner.write(self.headers.get('X-Deck-Code-Version')):
                payload = self.payload(36 * 1024 * 1024 if path == '/api/library/import' else MAX_BODY)
                if self.command == 'POST' and path in {'/api/selections', '/api/restore'}:
                    self.server.store.refresh_catalog()
                    data = self.server.store.update(payload, restore=path == '/api/restore')
                    self.json_response(200, {**data, 'selected_export': self.publish_selected()})
                elif self.command == 'POST' and path == '/api/selections/export':
                    result = self.publish_selected()
                    self.json_response(200 if result['status'] == 'complete' else 500, {'selected_export': result})
                elif self.command == 'POST' and path == '/api/exports':
                    self.json_response(202, self.server.production.submit(payload))
                elif self.command == 'PUT' and path == '/api/deck-config':
                    self.json_response(200, self.server.production.update(payload))
                elif self.command == 'POST' and path == '/api/library/import':
                    result = self.server.library.import_candidate(payload)
                    self.server.store.refresh_catalog()
                    self.json_response(201, result)
                elif self.command == 'PUT' and len(parts) == 4 and parts[:2] == ['api', 'library'] and parts[3] == 'layout':
                    self.json_response(200, self.server.library.update_layout(parts[2], payload))
                else:
                    self.json_response(404, {'error': '接口不存在。'})
        except PermissionError as error:
            self.json_response(409, {'error': str(error), 'code_fingerprint': self.server.owner.fingerprint})
        except (RevisionConflict, ConfigConflict) as error:
            self.json_response(409, {'error': str(error), 'current': error.current})
        except LibraryConflict as error:
            self.json_response(409, {'error': str(error)})
        except ValidationError as error:
            self.json_response(422, {'error': '制作素材或排版尚不完整。', 'errors': error.errors})
        except FileNotFoundError as error:
            self.json_response(404, {'error': str(error)})
        except (ValueError, UnicodeDecodeError, InvalidRequest) as error:
            self.json_response(400, {'error': str(error)})
        except OSError as error:
            self.json_response(500, {'error': str(error)})


def create_server(root=ROOT, port=8766, state_root=STATE, exports_root=EXPORTS, library_root=LIBRARY, start_worker=True, legacy_root=ROOT, publication_root=None):
    ui_root = project_path(root)
    state_root = project_path(state_root)
    library = Library(library_root)
    server = ThreadingHTTPServer(('127.0.0.1', port), ReviewHandler)
    try:
        server.owner = RuntimeOwner(state_root, server.server_port, legacy_root)
        server.ui_root = ui_root
        server.library = library
        server.publication_root = publication_root
        server.store = SelectionStore(state_root, library.catalog)
        server.production = Production(state_root, server.store, library.path, exports_root=exports_root,
                                       legacy_exports=[ROOT / 'exports'], start_worker=start_worker)
        try:
            server.selected_export = publish_selection(server.store.current(), server.production.registry.load(), publication_root)
        except (OSError, ValueError) as error:
            server.selected_export = {'status': 'failed', 'error': str(error)}
        return server
    except Exception:
        server.server_close()
        raise


def main():
    parser = argparse.ArgumentParser(description='AI 娘本地创作与制作工具')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--state-root', type=Path, default=STATE)
    parser.add_argument('--fingerprint', action='store_true')
    args = parser.parse_args()
    if args.fingerprint:
        print(code_fingerprint())
        return
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    with create_server(args.root, args.port, args.state_root) as server:
        print(f'AI 娘制作工具：http://127.0.0.1:{server.server_port}', flush=True)
        print(f'权威选择与配置：{server.store.root}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
