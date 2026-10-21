"""Cold-process check: native export dependencies must load before worker.start."""
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from deck import Production
from runtime_paths import PROJECT


def main():
    assert 'merchant_export' not in sys.modules
    assert 'numpy' not in sys.modules
    scratch = PROJECT / '04-制作工具/测试缓存'
    scratch.mkdir(exist_ok=True)
    observed = []
    with tempfile.TemporaryDirectory(prefix='export-preload-', dir=scratch) as directory:
        root = Path(directory).resolve()
        assert root.is_relative_to(scratch)
        (root / 'deck-config.json').write_bytes((PROJECT / '04-制作工具/webui/deck-config.json').read_bytes())
        def worker_start(thread):
            assert thread.name == 'deck-export'
            assert 'merchant_export' in sys.modules
            assert 'numpy._core._multiarray_umath' in sys.modules
            observed.append(thread.name)
        with patch.object(threading.Thread, 'start', worker_start):
            production = Production(root, object(), exports_root=root / 'exports')
        assert observed == ['deck-export']
        assert production.export_builder is sys.modules['merchant_export'].build_merchant_export
    print('Cold-process preload before export worker.start: passed')


if __name__ == '__main__':
    main()
