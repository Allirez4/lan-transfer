import asyncio
from contextlib import contextmanager
import hashlib
import sqlite3
import time

import anyio
import pytest
from fastapi.testclient import TestClient

from lan_transfer.api import PinnedFileResponse, create_app
from lan_transfer.auth import issue_code
from lan_transfer.shares import register_share, refresh_share, unshare
from lan_transfer.storage import version
from test_protocol import API, register, put, wait_done


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    pytest.fail('Condition did not become true')


@pytest.mark.parametrize('after_publish', [False, True])
def test_verifier_database_failure_recovers_without_service_shutdown(client, monkeypatch, after_publish):
    svc = client.app.state.service
    _, uid = register(client)
    put(client, uid, b'hello')
    broken = True
    execute = svc.repo.execute
    transaction = svc.repo.transaction

    def guarded_execute(sql, args=()):
        if broken and not after_publish and "SET state='finalizing'" in sql:
            raise sqlite3.OperationalError('database is locked')
        return execute(sql, args)

    @contextmanager
    def guarded_transaction():
        with transaction() as db:
            yield db
            if broken and after_publish and db.execute("SELECT state FROM uploads WHERE id=?", (uid,)).fetchone()[0] == 'completed':
                raise sqlite3.OperationalError('simulated completion commit failure')

    monkeypatch.setattr(svc.repo, 'execute', guarded_execute)
    monkeypatch.setattr(svc.repo, 'transaction', guarded_transaction)
    assert client.post(f'{API}/uploads/{uid}/complete', json={'sha256': hashlib.sha256(b'hello').hexdigest()}).status_code == 202
    eventually(lambda: svc.verification_error)
    assert client.get(API + '/health').status_code == 503
    assert client.get(API + '/capabilities').status_code == 200
    broken = False
    assert eventually(lambda: client.get(f'{API}/uploads/{uid}').json()['state'] == 'completed')
    eventually(lambda: client.get(API + '/health').status_code == 200)
    assert client.get(f'{API}/files/{uid}/content').content == b'hello'


def test_unpair_releases_reservations_and_local_admin_can_clean(client):
    svc = client.app.state.service
    svc.config.max_jobs = 1
    jid, uid = register(client, b'hello')
    put(client, uid, b'hello')
    assert client.delete(API + '/sessions/current').status_code == 200
    assert svc.repo.one('SELECT state FROM jobs WHERE id=?', (jid,))['state'] == 'canceled'
    assert svc.disk_status()['reserved'] == 0
    assert svc.part(uid).exists()
    token = client.post(API + '/pair', json={'client': 'cli', 'code': issue_code(svc.repo)}).json()['token']
    client.headers['Authorization'] = 'Bearer ' + token
    assert client.post(API + '/jobs', json={'key': 'new-device', 'name': 'new'}).status_code == 200
    assert client.delete(f'{API}/jobs/{jid}/incomplete').status_code == 403
    client.portal.call(svc.discard_incomplete, jid)
    assert not svc.part(uid).exists()


def test_completed_file_reconciles_failed_job_summary(client, monkeypatch):
    svc = client.app.state.service
    jid, uid = register(client)
    put(client, uid, b'hello')
    original = svc.refresh_job
    failed = False
    def refresh(job):
        nonlocal failed
        if not failed and svc.upload(uid)['state'] == 'completed':
            failed = True
            raise sqlite3.OperationalError('summary commit failed')
        return original(job)
    monkeypatch.setattr(svc, 'refresh_job', refresh)
    client.post(f'{API}/uploads/{uid}/complete', json={'sha256': hashlib.sha256(b'hello').hexdigest()})
    eventually(lambda: failed)
    eventually(lambda: client.get(f'{API}/jobs/{jid}').json()['state'] == 'completed')


def test_expired_device_is_rejected_and_jobs_are_reclaimed(client):
    svc = client.app.state.service
    jid, _ = register(client)
    owner = svc.repo.one('SELECT owner FROM jobs WHERE id=?', (jid,))['owner']
    svc.repo.execute('UPDATE sessions SET created=0 WHERE id=?', (owner,))
    assert client.get(API + '/capabilities').status_code == 401
    client.portal.call(svc.cleanup_orphans)
    assert svc.disk_status()['reserved'] == 0
    assert svc.repo.one('SELECT state FROM jobs WHERE id=?', (jid,))['state'] == 'canceled'


def test_manifest_quota_is_atomic_and_allows_exact_replay(client):
    svc = client.app.state.service
    svc.config.max_entries_per_job = 2
    jid = client.post(API + '/jobs', json={'key': 'quota', 'name': 'quota'}).json()['id']
    entries = [{'path': 'a', 'size': 0}, {'path': 'b', 'size': 0}]
    assert client.post(f'{API}/jobs/{jid}/manifest', json={'entries': entries}).status_code == 200
    assert client.post(f'{API}/jobs/{jid}/manifest', json={'entries': entries}).status_code == 200
    assert client.post(f'{API}/jobs/{jid}/manifest', json={'entries': [{'path': 'c', 'size': 0}]}).status_code == 413
    assert len(client.get(f'{API}/jobs/{jid}/entries').json()) == 2
    assert len(list(svc.stage.iterdir())) == 2


def test_configured_share_is_denied_after_root_removed(client, tmp_path):
    svc = client.app.state.service
    source = tmp_path / 'source.txt'
    source.write_text('private source')
    svc.config.share_roots = [str(tmp_path)]
    sid = register_share(svc.config, svc.repo, source)
    fid = client.get(f'{API}/shares/{sid}/manifest').json()['entries'][0]['id']
    svc.config.share_roots = []
    assert client.get(f'{API}/files/{fid}/content').status_code == 404
    assert client.get(f'{API}/shares/{sid}/manifest').status_code == 404
    assert not client.get(API + '/shares').json()


def test_native_selected_share_refresh_and_unshare_preserve_original(client, tmp_path):
    svc = client.app.state.service
    source = tmp_path / 'selected.txt'
    source.write_bytes(b'old')
    sid = register_share(svc.config, svc.repo, source, local_selection=True)
    old = client.get(f'{API}/shares/{sid}/manifest').json()['entries'][0]['id']
    source.write_bytes(b'new version')
    assert client.get(f'{API}/files/{old}/content').status_code == 409
    refresh_share(svc.config, svc.repo, sid)
    new = client.get(f'{API}/shares/{sid}/manifest').json()['entries'][0]['id']
    assert new != old and client.get(f'{API}/files/{old}/content').status_code == 404
    assert client.get(f'{API}/files/{new}/content').content == b'new version'
    unshare(svc.repo, sid)
    assert client.get(f'{API}/files/{new}/content').status_code == 404
    assert source.read_bytes() == b'new version'


def test_non_lan_client_rejected_before_pairing(config):
    with TestClient(create_app(config), client=('8.8.8.8', 50000)) as client:
        assert client.get(API + '/health').status_code == 403
        assert client.post(API + '/pair', json={'code': 'invalid', 'client': 'cli'}).status_code == 403


def test_download_disconnect_cancels_reader_and_releases_slot(tmp_path):
    path = tmp_path / 'download.bin'
    path.write_bytes(b'a' * (4 * 1024 * 1024))
    async def run():
        slots = anyio.Semaphore(1)
        disconnect = anyio.Event()
        messages = []
        async def receive():
            await disconnect.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            if message['type'] == 'http.response.body':
                messages.append(message)
                disconnect.set()
                await anyio.sleep(.05)
        with anyio.fail_after(2):
            await PinnedFileResponse(path, version(path), limiter=slots)({'type': 'http', 'method': 'GET', 'headers': []}, receive, send)
        assert len(messages) == 1
        assert slots.value == 1
    anyio.run(run)


def test_stalled_download_times_out_and_releases_slot(tmp_path):
    path = tmp_path / 'stalled.bin'
    path.write_bytes(b'test')
    async def run():
        slots = anyio.Semaphore(1)
        async def receive():
            await anyio.sleep_forever()
        async def send(message):
            await anyio.sleep_forever()
        with pytest.raises(TimeoutError):
            await PinnedFileResponse(path, version(path), limiter=slots, idle_seconds=.02)({'type': 'http', 'method': 'GET', 'headers': []}, receive, send)
        assert slots.value == 1
    anyio.run(run)
