import hashlib
import pytest

from test_protocol import API, put, register, wait_done
from test_client import new_job, remote_for
from lan_transfer.cli import register_share
from lan_transfer_client.state import State
from lan_transfer_client.transfer import download


def test_one_failed_file_does_not_block_other_files(client):
    # A healthy sibling must still finish when another file failed verification.
    jid = client.post(API + '/jobs', json={'key': 'mixed', 'name': 'mixed'}).json()['id']
    client.post(f'{API}/jobs/{jid}/manifest', json={'entries': [{'path': 'bad', 'size': 5}, {'path': 'good', 'size': 5}]})
    client.post(f'{API}/jobs/{jid}/seal')
    bad, good = client.get(f'{API}/jobs/{jid}/entries').json()
    put(client, bad['id'], b'hello')
    client.post(f"{API}/uploads/{bad['id']}/complete", json={'sha256': '0' * 64})
    assert wait_done(client, bad['id'])['state'] == 'failed'
    assert put(client, good['id'], b'hello').status_code == 200
    client.post(f"{API}/uploads/{good['id']}/complete", json={'sha256': hashlib.sha256(b'hello').hexdigest()})
    assert wait_done(client, good['id'])['state'] == 'completed'
    assert client.get(f'{API}/jobs/{jid}').json()['state'] == 'failed'


def test_pagination_keeps_equal_timestamp_records(client):
    repo = client.app.state.service.repo
    owner = client.get(API + '/sessions').json()['id']
    with repo.transaction() as db:
        for index in range(501):
            db.execute('INSERT INTO jobs(id,owner,key,name,created,state) VALUES (?,?,?,?,?,?)', (f'{index:032x}', owner, f'page-{index}', 'page', 123.0, 'completed'))
            db.execute('INSERT INTO shares VALUES (?,?,?,?)', (f'{index:032x}', 'page', 'unused', 123.0))
    for endpoint in ('jobs', 'shares'):
        first = client.get(f'{API}/{endpoint}?limit=500').json()
        last = first[-1]
        second = client.get(f"{API}/{endpoint}?limit=500&after={last['created']}&after_id={last['id']}").json()
        assert len(first) == 500 and len(second) == 1
        assert len({r['id'] for r in first + second}) == 501


def test_cli_download_rename_and_skip_persist(client, tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'one.txt').write_text('one')
    (source / 'two.txt').write_text('two')
    service = client.app.state.service
    service.config.share_roots = [str(tmp_path)]
    sid = register_share(service.config, service.repo, source)
    state = State(tmp_path / 'state.db')
    target = tmp_path / 'target'
    (target / 'source').mkdir(parents=True)
    (target / 'source' / 'one.txt').write_text('keep')
    remote = remote_for(client)
    job = new_job(state, 'download', destination=target, remote=sid)
    job['conflict_policy'] = 'rename'
    download(state, job, remote)
    assert (target / 'source' / 'one.txt').read_text() == 'keep'
    assert (target / 'source' / 'one (2).txt').read_text() == 'one'
    job['sealed'] = 1
    download(state, job, remote)
    assert not (target / 'source' / 'one (3).txt').exists()
    next_job = new_job(state, 'download', destination=target, remote=sid)
    next_job['conflict_policy'] = 'skip'
    download(state, next_job, remote)
    assert state.one("SELECT COUNT(*) count FROM entries WHERE job=? AND state='skipped'", (next_job['id'],))['count'] == 2
    state.close()


def test_nfd_source_name_can_be_shared_without_normalizing_the_source(client, tmp_path):
    path = tmp_path / 'cafe\u0301.txt'
    path.write_bytes(b'non-normalized original')
    svc = client.app.state.service
    svc.config.share_roots = [str(tmp_path)]
    sid = register_share(svc.config, svc.repo, path)
    entry = client.get(f'{API}/shares/{sid}/manifest').json()['entries'][0]
    assert client.get(f"{API}/files/{entry['id']}/content").content == path.read_bytes()


def test_fat32_rejects_large_files_before_registration(client):
    service = client.app.state.service
    service.filesystem = 'FAT32'
    jid = client.post(API + '/jobs', json={'key': 'fat', 'name': 'fat'}).json()['id']
    response = client.post(f'{API}/jobs/{jid}/manifest', json={'entries': [{'path': 'large.bin', 'size': 2**32}]})
    assert response.status_code == 422 and 'FAT32' in response.text
    assert client.get(f'{API}/jobs/{jid}/entries').json() == []
    from lan_transfer.storage import check_file_size
    check_file_size(2**32 - 1, 'FAT32')
    with pytest.raises(ValueError, match='FAT32'):
        check_file_size(2**32, 'FAT32')


def test_discard_requires_cancellation_and_preserves_completed_files(client):
    jid = client.post(API + '/jobs', json={'key': 'discard', 'name': 'discard'}).json()['id']
    client.post(f'{API}/jobs/{jid}/manifest', json={'entries': [{'path': 'done', 'size': 5}, {'path': 'partial', 'size': 5}]})
    client.post(f'{API}/jobs/{jid}/seal')
    done, partial = client.get(f'{API}/jobs/{jid}/entries').json()
    put(client, done['id'], b'hello')
    client.post(f"{API}/uploads/{done['id']}/complete", json={'sha256': hashlib.sha256(b'hello').hexdigest()})
    assert wait_done(client, done['id'])['state'] == 'completed'
    put(client, partial['id'], b'world')
    assert client.delete(f'{API}/jobs/{jid}/incomplete').status_code == 409
    client.post(f'{API}/jobs/{jid}/cancel')
    assert client.delete(f'{API}/jobs/{jid}/incomplete').json()['discarded'] == 1
    assert not client.app.state.service.part(partial['id']).exists()
    assert client.get(f"{API}/files/{done['id']}/content").content == b'hello'
    assert client.delete(f'{API}/jobs/{jid}/incomplete').status_code == 200


def test_live_truncated_staging_file_cannot_create_a_hole(client):
    chunk = 4 * 1024**2
    jid, uid = register(client, data=b'x' * (chunk + 5))
    assert put(client, uid, b'x' * chunk).status_code == 200
    client.app.state.service.part(uid).write_bytes(b'x')
    assert put(client, uid, b'hello', offset=chunk, rid='next').status_code == 409
    assert client.get(f'{API}/uploads/{uid}').json()['state'] == 'repair_required'
