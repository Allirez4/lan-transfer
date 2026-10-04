import hashlib

from test_protocol import API, put, wait_done


def create(client, entries):
    jid = client.post(API + '/jobs', json={'key': 'conflicts', 'name': 'conflicts', 'expected_entries': len(entries)}).json()['id']
    response = client.post(f'{API}/jobs/{jid}/manifest', json={'allow_conflicts': True, 'entries': entries})
    assert response.status_code == 200, response.text
    return jid, client.get(f'{API}/jobs/{jid}/entries').json()


def test_rename_mapping_survives_page_replay(client):
    entries = [{'key': 'first', 'path': 'one.txt', 'size': 5}, {'key': 'second', 'path': 'ONE.txt', 'size': 5}]
    jid, rows = create(client, entries)
    conflict = rows[1]
    assert conflict['state'] == 'conflict' and conflict['suggested_path'] == 'ONE (2).txt'
    assert client.post(f'{API}/jobs/{jid}/seal').status_code == 409
    result = client.post(f"{API}/uploads/{conflict['id']}/resolve", json={'action': 'rename', 'path': 'renamed.txt'})
    assert result.status_code == 200
    assert client.post(f'{API}/jobs/{jid}/manifest', json={'allow_conflicts': True, 'entries': entries}).status_code == 200
    rows = client.get(f'{API}/jobs/{jid}/entries').json()
    assert rows[1]['path'] == 'renamed.txt' and rows[1]['source_path'] == 'ONE.txt' and rows[1]['source_key'] == 'second'
    assert client.post(f'{API}/jobs/{jid}/seal').status_code == 200
    for row in rows:
        assert put(client, row['id'], b'hello').status_code == 200
        client.post(f"{API}/uploads/{row['id']}/complete", json={'sha256': hashlib.sha256(b'hello').hexdigest()})
        assert wait_done(client, row['id'])['state'] == 'completed'


def test_skip_is_explicit_and_batch_can_complete(client):
    jid, rows = create(client, [{'key': 'a', 'path': 'same', 'size': 0}, {'key': 'b', 'path': 'SAME', 'size': 100}])
    assert client.post(f"{API}/uploads/{rows[1]['id']}/resolve", json={'action': 'skip'}).status_code == 200
    assert client.post(f'{API}/jobs/{jid}/seal').status_code == 200
    client.post(f"{API}/uploads/{rows[0]['id']}/complete", json={'sha256': hashlib.sha256(b'').hexdigest()})
    assert wait_done(client, rows[0]['id'])['state'] == 'completed'
    assert client.get(f'{API}/jobs/{jid}').json()['state'] == 'completed'
    assert client.get(f"{API}/uploads/{rows[1]['id']}").json()['state'] == 'skipped'
    assert client.get(f'{API}/capabilities').json()['disk']['reserved'] == 0


def test_directory_rename_moves_descendants_and_future_pages(client):
    entries = [{'path': 'Tree', 'kind': 'directory', 'size': 0}, {'path': 'Tree/a', 'size': 0},
               {'path': 'tree', 'kind': 'directory', 'size': 0}, {'path': 'tree/b', 'size': 0}]
    jid, rows = create(client, entries)
    assert rows[2]['state'] == rows[3]['state'] == 'conflict'
    assert client.post(f"{API}/uploads/{rows[2]['id']}/resolve", json={'action': 'rename', 'path': 'tree (2)'}).status_code == 200
    rows = client.get(f'{API}/jobs/{jid}/entries').json()
    assert rows[3]['path'] == 'tree (2)/b' and rows[3]['state'] == 'queued'
    assert client.post(f'{API}/jobs/{jid}/manifest', json={'allow_conflicts': True, 'entries': [{'path': 'tree/c', 'size': 0}]}).status_code == 200
    assert client.get(f'{API}/jobs/{jid}/entries').json()[-1]['path'] == 'tree (2)/c'
    # Expected count also rejects accidental extras, rather than silently claiming a faithful tree.
    assert client.post(f'{API}/jobs/{jid}/seal').status_code == 409


def test_incomplete_manifest_cannot_seal(client):
    jid = client.post(API + '/jobs', json={'key': 'incomplete', 'name': 'incomplete', 'expected_entries': 2}).json()['id']
    client.post(f'{API}/jobs/{jid}/manifest', json={'entries': [{'path': 'one', 'size': 0}]})
    assert client.post(f'{API}/jobs/{jid}/seal').status_code == 409
    assert client.get(f'{API}/jobs/{jid}').json()['state'] == 'registering'
