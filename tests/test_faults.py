from process_utils import stop_tree
import asyncio
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest
from starlette.requests import ClientDisconnect

from test_protocol import API, put, register, wait_done
from lan_transfer.auth import issue_code
from lan_transfer.repository import Repository


def test_disconnect_mid_chunk_preserves_offset(client):
    _, uid = register(client)
    svc = client.app.state.service
    owner = client.get(API + '/sessions').json()['id']
    class Disconnected:
        async def stream(self):
            yield b'hel'
            raise ClientDisconnect()
    with pytest.raises(ClientDisconnect):
        client.portal.call(svc.append, uid, owner, Disconnected(), 0, 5, hashlib.sha256(b'hello').hexdigest(), 'disconnect')
    assert svc.part(uid).read_bytes() == b''
    assert svc.upload(uid)['offset'] == 0
    assert put(client, uid, b'hello').status_code == 200


def test_failed_fsync_does_not_commit(client, monkeypatch):
    _, uid = register(client)
    original = os.fsync
    calls = 0
    def full(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.ENOSPC, 'No space left on device')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', full)
    assert put(client, uid, b'hello').status_code == 507
    svc = client.app.state.service
    assert svc.upload(uid)['offset'] == 0
    assert svc.part(uid).read_bytes() == b''
    assert svc.repo.all('SELECT * FROM receipts WHERE upload=?', (uid,)) == []


def test_failed_db_commit_rolls_back_file(client, monkeypatch):
    _, uid = register(client)
    svc = client.app.state.service
    original = svc.repo.transaction
    from contextlib import contextmanager
    @contextmanager
    def failure():
        with original() as db:
            yield db
            raise OSError(errno.EIO, 'Simulated metadata commit failure')
    monkeypatch.setattr(svc.repo, 'transaction', failure)
    assert put(client, uid, b'hello').status_code == 507
    assert svc.part(uid).read_bytes() == b''
    assert svc.upload(uid)['offset'] == 0


def test_whole_checksum_mismatch_never_publishes(client):
    jid, uid = register(client)
    put(client, uid, b'hello')
    client.post(f'{API}/uploads/{uid}/complete', json={'sha256': '0' * 64})
    assert wait_done(client, uid)['state'] == 'failed'
    assert client.get(f'{API}/files/{uid}/content').status_code == 404
    assert client.get(f'{API}/shares/{jid}/manifest').json()['state'] == 'failed'


def test_pairing_single_use_rate_limit_and_host(client):
    svc = client.app.state.service
    client.headers.pop('Authorization')
    code = issue_code(svc.repo)
    assert client.post(API + '/pair', json={'code': code, 'client': 'cli'}).status_code == 200
    assert client.post(API + '/pair', json={'code': code, 'client': 'cli'}).status_code == 401
    for _ in range(12):
        result = client.post(API + '/pair', json={'code': 'BAD', 'client': 'cli'})
    assert result.status_code == 429 and result.headers['retry-after'] == '300'
    assert client.get(API + '/health', headers={'Host': 'attacker.test'}).status_code == 400


def test_paginated_manifests_and_zero_file_size_limit(client):
    jid = client.post(API + '/jobs', json={'key': 'many', 'name': 'many'}).json()['id']
    for start in (0, 500, 1000):
        entries = [{'path': f'dir/file-{i}', 'size': 10_000_000_000 if i == 0 else 0} for i in range(start, start + 500)]
        # Admission may reject 10 GB on small CI disks; use a zero-byte fixture for that case.
        if start == 0:
            import shutil
            if shutil.disk_usage(client.app.state.service.config.storage).free < 11_000_000_000:
                entries[0]['size'] = 0
        result = client.post(f'{API}/jobs/{jid}/manifest', json={'entries': entries})
        assert result.status_code == 200, result.text
    first = client.get(f'{API}/jobs/{jid}/entries').json()
    second = client.get(f"{API}/jobs/{jid}/entries?after={first[-1]['seq']}").json()
    third = client.get(f"{API}/jobs/{jid}/entries?after={second[-1]['seq']}").json()
    assert len(first) == len(second) == len(third) == 500
    assert len({r['id'] for r in first + second + third}) == 1500
    assert client.post(f'{API}/jobs/{jid}/manifest', json={'entries': [{'path': str(i), 'size': 0} for i in range(501)]}).status_code == 422


def test_real_process_kill_mid_chunk_and_resume(tmp_path):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    storage = tmp_path / 'live-storage'
    storage.mkdir()
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'storage': str(storage), 'hosts': ['127.0.0.1'], 'origins': [f'http://127.0.0.1:{port}'], 'chunk_size': 4 * 1024 * 1024, 'safety_bytes': 0}), encoding='utf-8')
    repo = Repository(storage / 'state.sqlite3')
    code = issue_code(repo)
    repo.close()
    env = {**os.environ, 'LAN_TRANSFER_CONFIG': str(config), 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'backend')}
    def launch():
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', str(port), '--no-access-log'],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        for _ in range(100):
            try:
                if httpx.get(f'http://127.0.0.1:{port}{API}/health', timeout=.5).status_code == 200:
                    return process
            except httpx.TransportError:
                pass
            if process.poll() is not None:
                pytest.fail('Live test server failed to start')
            time.sleep(.05)
        stop_tree(process)
        pytest.fail('Live test server startup timed out')
    process = launch()
    connection = None
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}') as client:
            token = client.post(API + '/pair', json={'code': code, 'client': 'cli'}).json()['token']
            client.headers['Authorization'] = 'Bearer ' + token
            body = os.urandom(4 * 1024 * 1024)
            jid, uid = register(client, body)
            connection = socket.create_connection(('127.0.0.1', port))
            header = (f'PUT {API}/uploads/{uid}/data HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {token}\r\nContent-Type: application/octet-stream\r\nContent-Length: {len(body)}\r\nUpload-Offset: 0\r\nUpload-Length: {len(body)}\r\nUpload-SHA256: {hashlib.sha256(body).hexdigest()}\r\nUpload-Request-ID: killed\r\n\r\n')
            connection.sendall(header.encode() + body[:1024 * 1024])
            part = storage / '.staging' / (uid + '.part')
            for _ in range(100):
                if part.stat().st_size:
                    break
                time.sleep(.01)
            assert part.stat().st_size > 0
            stop_tree(process)
            connection.close(); connection = None
            process = launch()
            assert part.stat().st_size == 0
            assert client.get(f'{API}/uploads/{uid}').json()['offset'] == 0
            assert put(client, uid, body).status_code == 200
            # Kill after commit, without relying on any in-memory offset state.
            stop_tree(process)
            process = launch()
            assert put(client, uid, body).json()['replayed'] is True
            client.post(f'{API}/uploads/{uid}/complete', json={'sha256': hashlib.sha256(body).hexdigest()})
            assert wait_done(client, uid)['state'] == 'completed'
            assert client.get(f'{API}/files/{uid}/content').content == body
    finally:
        if connection:
            connection.close()
        stop_tree(process)
