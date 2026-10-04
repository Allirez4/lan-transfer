import hashlib
import os
from pathlib import Path
import secrets

import httpx
import pytest

from lan_transfer.cli import register_share
from lan_transfer.storage import digest_file
from lan_transfer_client.state import State
from lan_transfer_client.transfer import BLOCK, Remote, download, upload, validate_range, verify_checkpoint


def remote_for(client):
    remote = Remote.__new__(Remote)
    remote.client = client
    client.base_url = httpx.URL('http://testserver/api/v1/')
    return remote


def new_job(state, direction, source=None, destination=None, remote=None):
    jid = secrets.token_hex(16)
    state.execute("INSERT INTO jobs(id,direction,server,source,destination,remote) VALUES (?,?,?,?,?,?)", (jid, direction, 'http://testserver', str(source) if source else None, str(destination) if destination else None, remote))
    return state.one("SELECT * FROM jobs WHERE id=?", (jid,))


def test_recursive_upload_download_checkpoint_resume(client, tmp_path, monkeypatch):
    source = tmp_path / 'درخت'
    (source / 'empty' / 'nested').mkdir(parents=True)
    (source / 'sub').mkdir()
    (source / 'sub' / 'big.bin').write_bytes(os.urandom(BLOCK + 710))
    (source / '.hidden').write_bytes(b'hidden')
    (source / 'zero').touch()
    (source / 'سلام.txt').write_text('درود', encoding='utf-8')
    state = State(tmp_path / 'client.sqlite')
    remote = remote_for(client)
    job = new_job(state, 'upload', source=source)
    upload(state, job, remote)
    registered = state.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
    assert registered['state'] == 'completed'
    target = tmp_path / 'download'
    job = new_job(state, 'download', destination=target, remote=registered['remote'])
    original = state.execute
    interrupted = False
    def checkpoint_then_kill(sql, args=()):
        nonlocal interrupted
        original(sql, args)
        if 'SET offset=' in sql and args[0] == BLOCK and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()
    monkeypatch.setattr(state, 'execute', checkpoint_then_kill)
    with pytest.raises(KeyboardInterrupt):
        download(state, job, remote)
    partial = state.one("SELECT * FROM entries WHERE job=? AND offset=?", (job['id'], BLOCK))
    assert partial is not None
    state.close()
    state = State(tmp_path / 'client.sqlite')
    job = state.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
    download(state, job, remote)
    assert state.one('SELECT state FROM jobs WHERE id=?', (job['id'],))['state'] == 'completed'
    for path in source.rglob('*'):
        copied = target / path.relative_to(source.parent)
        if path.is_dir():
            assert copied.is_dir()
        else:
            assert copied.stat().st_size == path.stat().st_size
            assert digest_file(copied) == digest_file(path)
    # Completed files verify locally and are never retransmitted on subsequent resumes.
    def unexpected_stream(*args, **kwargs):
        pytest.fail('Completed file was downloaded again')
    monkeypatch.setattr(remote.client, 'stream', unexpected_stream)
    download(state, job, remote)
    state.close()


@pytest.mark.parametrize('status,headers', [
    (200, {}), (416, {'content-range': 'bytes */10'}),
    (206, {'content-range': 'bytes 0-4/10', 'content-length': '5', 'etag': '"v"'}),
    (206, {'content-range': 'bytes 5-9/10', 'content-length': '5', 'etag': '"changed"'}),
    (206, {'content-range': 'bytes 5-9/10', 'content-length': '10', 'etag': '"v"'}),
])
def test_reject_unsafe_range_responses(status, headers):
    response = httpx.Response(status, headers=headers, request=httpx.Request('GET', 'http://test/content'))
    with pytest.raises(ValueError):
        validate_range(response, 5, 9, 10, 'v')


def test_checkpoint_truncate_and_corruption(tmp_path):
    path = tmp_path / 'partial'
    path.write_bytes(b'hello-uncommitted')
    expected = hashlib.sha256(b'hello').hexdigest()
    assert verify_checkpoint(path, 5, expected).hexdigest() == expected
    assert path.read_bytes() == b'hello'
    path.write_bytes(b'wrong')
    with pytest.raises(ValueError, match='checksum mismatch'):
        verify_checkpoint(path, 5, expected)
    path.write_bytes(b'he')
    with pytest.raises(ValueError, match='shorter'):
        verify_checkpoint(path, 5, expected)


def test_live_download_retry_uses_checkpoint_without_rereading_prefix(client, tmp_path, monkeypatch):
    from contextlib import contextmanager
    import lan_transfer_client.transfer as module
    source = tmp_path / 'large.bin'
    source.write_bytes(os.urandom(BLOCK + 2 * 1024 * 1024 + 7))
    svc = client.app.state.service
    svc.config.share_roots = [str(tmp_path)]
    sid = register_share(svc.config, svc.repo, source)
    state = State(tmp_path / 'retry.sqlite')
    job = new_job(state, 'download', destination=tmp_path / 'result', remote=sid)
    remote = remote_for(client)
    original_stream = remote.client.stream
    original_verify = module.verify_checkpoint
    checkpoints, ranges = [], []
    def count_checkpoint(*args):
        checkpoints.append(args[1])
        return original_verify(*args)
    @contextmanager
    def interrupted_stream(*args, **kwargs):
        ranges.append(kwargs['headers']['Range'])
        with original_stream(*args, **kwargs) as response:
            iterator = response.iter_bytes
            if len(ranges) == 1:
                def truncate(chunk_size):
                    consumed = 0
                    for block in iterator(chunk_size):
                        yield block
                        consumed += len(block)
                        if consumed >= BLOCK + 1024 * 1024:
                            raise httpx.ReadError('Wi-Fi interrupted after a durable checkpoint')
                response.iter_bytes = truncate
            yield response
    monkeypatch.setattr(module, 'verify_checkpoint', count_checkpoint)
    monkeypatch.setattr(remote.client, 'stream', interrupted_stream)
    try:
        download(state, job, remote)
        assert checkpoints == [0]
        assert ranges == [f'bytes=0-{source.stat().st_size - 1}', f'bytes={BLOCK}-{source.stat().st_size - 1}']
        assert (tmp_path / 'result' / 'large.bin').read_bytes() == source.read_bytes()
    finally:
        state.close()


def test_local_share_without_copy_and_change_detection(client, tmp_path):
    source = tmp_path / 'local'
    source.mkdir()
    path = source / 'original.bin'
    path.write_bytes(b'original data')
    svc = client.app.state.service
    svc.config.share_roots = [str(source)]
    sid = register_share(svc.config, svc.repo, path)
    file = client.get(f'/api/v1/shares/{sid}/manifest').json()['entries'][0]
    assert not list(svc.stage.iterdir())
    assert client.get(f"/api/v1/files/{file['id']}/metadata").json()['sha256'] == hashlib.sha256(b'original data').hexdigest()
    assert client.get(f"/api/v1/files/{file['id']}/content").content == b'original data'
    path.write_bytes(b'changed bytes')
    assert client.get(f"/api/v1/files/{file['id']}/content").status_code == 409
    assert client.get(f"/api/v1/files/{file['id']}/metadata").status_code == 409
    with pytest.raises(ValueError, match='outside'):
        register_share(svc.config, svc.repo, tmp_path / 'outside')


def test_download_does_not_replace_existing(client, tmp_path):
    path = tmp_path / 'source.bin'
    path.write_bytes(b'new content')
    svc = client.app.state.service
    svc.config.share_roots = [str(tmp_path)]
    sid = register_share(svc.config, svc.repo, path)
    destination = tmp_path / 'target'
    destination.mkdir()
    (destination / 'source.bin').write_bytes(b'keep me')
    state = State(tmp_path / 'state.sqlite')
    job = new_job(state, 'download', destination=destination, remote=sid)
    with pytest.raises(FileExistsError):
        download(state, job, remote_for(client))
    assert (destination / 'source.bin').read_bytes() == b'keep me'
    state.close()
