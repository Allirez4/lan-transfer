import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from lan_transfer.api import create_app
from lan_transfer.auth import issue_code, hashed
from lan_transfer.service import Service
from lan_transfer.storage import ProcessLock, digest_file

API = "/api/v1"


def register(client, data=b"hello", path="test.bin", key="test", seal=True):
    job = client.post(API + "/jobs", json={"key": key, "name": key}).json()
    response = client.post(f"{API}/jobs/{job['id']}/manifest", json={"entries": [{"path": path, "size": len(data)}]})
    assert response.status_code == 200, response.text
    if seal:
        assert client.post(f"{API}/jobs/{job['id']}/seal").status_code == 200
    entry = client.get(f"{API}/jobs/{job['id']}/entries").json()[0]
    return job["id"], entry["id"]


def put(client, uid, body, offset=0, rid="one", digest=None, length=None):
    return client.put(f"{API}/uploads/{uid}/data", content=body, headers={"Content-Type": "application/octet-stream", "Upload-Offset": str(offset), "Upload-Length": str(len(body) if length is None else length), "Upload-SHA256": digest or hashlib.sha256(body).hexdigest(), "Upload-Request-ID": rid})


def wait_done(client, uid):
    for _ in range(100):
        row = client.get(f"{API}/uploads/{uid}").json()
        if row["state"] in ("completed", "failed", "repair_required"):
            return row
        time.sleep(0.03)
    pytest.fail("Verification did not finish")


def test_real_transfer_ranges_and_integrity(client):
    data = os.urandom(4 * 1024 * 1024 + 725)
    job, uid = register(client, data, "پوشه/سلام.bin")
    first = data[:4 * 1024 * 1024]
    assert put(client, uid, first).json()["offset"] == len(first)
    assert put(client, uid, first).json()["replayed"] is True
    assert put(client, uid, data[-725:], len(first), "two").status_code == 200
    assert client.post(f"{API}/uploads/{uid}/complete", json={"sha256": hashlib.sha256(data).hexdigest()}).status_code == 202
    assert wait_done(client, uid)["state"] == "completed"
    url = f"{API}/files/{uid}/content"
    result = client.get(url)
    assert result.content == data
    assert "attachment" in result.headers["content-disposition"]
    assert result.headers["content-type"] == "application/octet-stream"
    head = client.head(url)
    assert not head.content and int(head.headers["content-length"]) == len(data)
    etag = head.headers["etag"]
    response = client.get(url, headers={"Range": "bytes=12-31", "If-Range": etag})
    assert response.status_code == 206 and response.content == data[12:32]
    assert response.headers["content-range"] == f"bytes 12-31/{len(data)}"
    assert response.headers["content-length"] == "20"
    assert client.get(url, headers={"Range": "bytes=12-31", "If-Range": '"changed"'}).status_code == 200
    response = client.get(url, headers={"Range": f"bytes={len(data)}-"})
    assert response.status_code == 416 and response.headers["content-range"] == f"bytes */{len(data)}"
    assert client.get(f"{API}/shares/{job}/manifest").json()["state"] == "completed"


@pytest.mark.parametrize("mode", ["wrong_hash", "short", "long", "offset"])
def test_bad_chunk_does_not_advance(client, mode):
    _, uid = register(client)
    response = put(client, uid, b"hello" if mode != "long" else b"hello!", offset=1 if mode == "offset" else 0,
                   digest="0" * 64 if mode == "wrong_hash" else None, length=5 if mode != "short" else 6)
    assert response.status_code in (409, 422)
    assert client.get(f"{API}/uploads/{uid}").json()["offset"] == 0
    assert client.app.state.service.part(uid).stat().st_size == 0


def test_truncated_stream_rollback(client):
    _, uid = register(client)
    assert put(client, uid, b"hel", length=5).status_code == 422
    assert client.app.state.service.part(uid).read_bytes() == b""


def test_competing_writers(client):
    _, uid = register(client)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda rid: put(client, uid, b"hello", rid=rid).status_code, ["a", "b"]))
    assert sorted(results) == [200, 409]
    assert client.app.state.service.part(uid).read_bytes() == b"hello"


@pytest.mark.parametrize("name", ["../bad", "/absolute", "C:/a", "a\\b", "a/CON.txt", "a/stream:bad", "a./b", "a//b", "a/\x00x", "LPT1", "a/../b"])
def test_unsafe_paths(client, name):
    jid = client.post(API + "/jobs", json={"key": name, "name": "bad"}).json()["id"]
    result = client.post(f"{API}/jobs/{jid}/manifest", json={"entries": [{"path": name, "size": 1}]})
    assert result.status_code == 422
    assert client.get(f"{API}/jobs/{jid}/entries").json() == []


def test_manifest_case_collision_and_idempotence(client):
    jid, _ = register(client, path="Folder/a", seal=False)
    same = {"entries": [{"path": "Folder/a", "size": 5}]}
    assert client.post(f"{API}/jobs/{jid}/manifest", json=same).status_code == 200
    assert client.post(f"{API}/jobs/{jid}/manifest", json={"entries": [{"path": "folder/b", "size": 1}]}).status_code == 409
    assert client.post(f"{API}/jobs/{jid}/manifest", json={"entries": [{"path": "Folder", "size": 1}]}).status_code == 409


def test_zero_file_and_empty_directories(client):
    jid, uid = register(client, b"", seal=False)
    assert client.post(f"{API}/jobs/{jid}/manifest", json={"entries": [{"path": "empty/nested", "kind": "directory", "size": 0}]}).status_code == 200
    client.post(f"{API}/jobs/{jid}/seal")
    client.post(f"{API}/uploads/{uid}/complete", json={"sha256": hashlib.sha256(b"").hexdigest()})
    assert wait_done(client, uid)["state"] == "completed"
    assert (client.app.state.service.received / jid / "empty" / "nested").is_dir()


def test_auth_origin_expiry_and_revocation(client):
    svc = client.app.state.service
    client.headers.pop("Authorization")
    for path in ("/jobs", "/shares", "/capabilities", "/uploads/unknown", "/files/unknown/content"):
        assert client.get(API + path).status_code == 401
    code = issue_code(svc.repo)
    svc.repo.execute("UPDATE pairing SET expires=0 WHERE hash=?", (hashed(code),))
    assert client.post(API + "/pair", json={"code": code, "client": "cli"}).status_code == 401
    code = issue_code(svc.repo)
    assert client.post(API + "/pair", json={"code": code}, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post(API + "/jobs", json={"key": "x", "name": "x"}).status_code == 403
    assert client.post(API + "/jobs", json={"key": "x", "name": "x"}, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.delete(API + "/sessions/current", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.get(API + "/jobs").status_code == 401


def test_restart_suffix_and_short_file(config):
    with TestClient(create_app(config), client=("127.0.0.1", 50000)) as c:
        token = c.post(API + "/pair", json={"code": issue_code(c.app.state.service.repo), "client": "cli"}).json()["token"]
        c.headers["Authorization"] = "Bearer " + token
        _, uid = register(c)
        assert put(c, uid, b"hello").status_code == 200
        path = c.app.state.service.part(uid)
    with path.open("ab") as f:
        f.write(b"uncommitted")
    with TestClient(create_app(config), client=("127.0.0.1", 50000)) as c:
        c.headers["Authorization"] = "Bearer " + token
        assert path.read_bytes() == b"hello"
        assert put(c, uid, b"hello").json()["replayed"] is True
    path.write_bytes(b"he")
    with TestClient(create_app(config), client=("127.0.0.1", 50000)) as c:
        c.headers["Authorization"] = "Bearer " + token
        assert c.get(f"{API}/uploads/{uid}").json()["state"] == "repair_required"


def test_restart_after_final_move(config):
    with TestClient(create_app(config), client=("127.0.0.1", 50000)) as c:
        token = c.post(API + "/pair", json={"code": issue_code(c.app.state.service.repo), "client": "cli"}).json()["token"]
        c.headers["Authorization"] = "Bearer " + token
        jid, uid = register(c)
        put(c, uid, b"hello")
        svc = c.app.state.service
        svc.repo.execute("UPDATE jobs SET state='paused' WHERE id=?", (jid,))
        svc.repo.execute("UPDATE uploads SET state='finalizing',sha256=? WHERE id=?", (hashlib.sha256(b"hello").hexdigest(), uid))
        destination = svc.received / jid / "test.bin"
        os.rename(svc.part(uid), destination)
    with TestClient(create_app(config), client=("127.0.0.1", 50000)) as c:
        c.headers["Authorization"] = "Bearer " + token
        c.post(f"{API}/jobs/{jid}/resume")
        assert wait_done(c, uid)["state"] == "completed"
        assert destination.read_bytes() == b"hello"


def test_second_server_rejected(client):
    with pytest.raises((RuntimeError, OSError)):
        ProcessLock(client.app.state.service.config.storage / "server.lock")


def test_disk_admission(client, monkeypatch):
    from collections import namedtuple
    import lan_transfer.service as module
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: usage(10, 9, 1))
    jid = client.post(API + "/jobs", json={"key": "disk", "name": "disk"}).json()["id"]
    assert client.post(f"{API}/jobs/{jid}/manifest", json={"entries": [{"path": "big", "size": 100}]}).status_code == 507
    assert client.get(f"{API}/jobs/{jid}/entries").json() == []
