"""Loopback-only, disposable service for automated browser tests."""
from pathlib import Path
import tempfile
import time

import uvicorn
from lan_transfer.api import create_app
from lan_transfer.auth import hashed
from lan_transfer.config import Config
from lan_transfer.repository import Repository
from lan_transfer.service import Service

original_verify = Service.verify
def delayed_fixture_verify(self, uid):
    if self.upload(uid)['path'] == 'slow-verify.bin':
        time.sleep(5)
    return original_verify(self, uid)
Service.verify = delayed_fixture_verify


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='lan-transfer-browser-') as temp:
        root = Path(temp)
        repo = Repository(root / 'state.sqlite3')
        # Multiple single-use fixture codes support isolated tests without an admin HTTP endpoint.
        for index in range(20):
            repo.execute('INSERT INTO pairing VALUES (?,?)', (hashed(f'TEST{index:04}'), time.time() + 600))
        repo.close()
        config = Config(storage=root, hosts=['127.0.0.1'], origins=['http://127.0.0.1:8765'], bind='127.0.0.1', port=8765, safety_bytes=0, chunk_size=4 * 1024 * 1024)
        uvicorn.run(create_app(config), host='127.0.0.1', port=8765, access_log=False)
