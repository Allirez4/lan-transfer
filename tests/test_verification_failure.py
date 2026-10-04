import errno

from test_protocol import API, put, register, wait_done


def test_final_directory_failure_marks_file_failed_without_stopping_server(client, monkeypatch):
    _, uid = register(client, path='nested/file')
    assert put(client, uid, b'hello').status_code == 200
    import lan_transfer.service as module
    original = module.confined
    def fail_mkdir(root, name, mkdir=False):
        if mkdir:
            raise OSError(errno.ENOSPC, 'Disk full creating final directory')
        return original(root, name, mkdir)
    monkeypatch.setattr(module, 'confined', fail_mkdir)
    import hashlib
    client.post(f'{API}/uploads/{uid}/complete', json={'sha256': hashlib.sha256(b'hello').hexdigest()})
    assert wait_done(client, uid)['state'] == 'failed'
    assert client.get(API + '/health').status_code == 200
