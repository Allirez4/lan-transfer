import hashlib
import json
import os
from pathlib import Path
import random
import re
import secrets
import time
import sys
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from itertools import islice

import httpx

from lan_transfer.storage import check_file_size, filesystem_name, confined, digest_file, fsync_dir, is_link, publish, relative_path, version

BLOCK = 16 * 1024 * 1024


class Remote:
    def __init__(self, server, token, ca=None):
        import ssl
        self.client = httpx.Client(base_url=server.rstrip('/') + '/api/v1/', headers={"Authorization": "Bearer " + token},
                                   timeout=httpx.Timeout(600, connect=15), verify=ssl.create_default_context(cafile=ca), follow_redirects=False, trust_env=False)

    def request(self, method, path, **kwargs):
        last = None
        for attempt in range(9):
            try:
                sending = dict(kwargs)
                if isinstance(kwargs.get('content'), bytes):
                    # HTTPX responses contain reference cycles. An exhausted generator
                    # releases the chunk promptly instead of retaining request bytes until GC.
                    def body_stream(data):
                        yield data
                    sending['content'] = body_stream(kwargs['content'])
                    sending['headers'] = {**kwargs.get('headers', {}), 'Content-Length': str(len(kwargs['content']))}
                result = self.client.request(method, path, **sending)
                if result.status_code in (408, 429, 500, 502, 503, 504):
                    delay = max(float(result.headers.get("retry-after", 0)), min(30, 2**attempt) * random.uniform(.75, 1.25))
                    if attempt == 8:
                        result.raise_for_status()
                    time.sleep(delay)
                    continue
                result.raise_for_status()
                return result.json()
            except httpx.TransportError as exc:
                last = exc
                if attempt < 8:
                    time.sleep(min(30, 2**attempt) * random.uniform(.75, 1.25))
        raise RuntimeError("Connection could not recover. Resume this job later.") from last

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, json=body or {})

    def close(self):
        self.client.close()


def safe_id(value):
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid server identifier")
    return value


def walk_source(source):
    source = Path(source).absolute()
    if any(is_link(p) for p in [source, *source.parents]):
        raise ValueError("Symlinks/junctions are not supported")
    if source.is_file():
        s = source.stat()
        yield {"path": source.name, "kind": "file", "size": s.st_size, "modified": s.st_mtime_ns // 1_000_000}
        return
    if not source.is_dir():
        raise ValueError("Source must be an ordinary file or directory")
    def walk_error(error):
        raise error
    for base, dirs, files in os.walk(source, followlinks=False, onerror=walk_error):
        base = Path(base)
        for name in [*dirs, *files]:
            if is_link(base / name):
                raise ValueError(f"Symlinks/junctions are not supported: {base / name}")
        relative = base.relative_to(source.parent).as_posix()
        yield {"path": relative, "kind": "directory", "size": 0, "modified": 0}
        for name in sorted(files):
            p = base / name
            if not p.is_file():
                raise ValueError(f"Not an ordinary file: {p}")
            s = p.stat()
            yield {"path": p.relative_to(source.parent).as_posix(), "kind": "file", "size": s.st_size, "modified": s.st_mtime_ns // 1_000_000}



def conflict_choice(policy, label):
    if policy in ('rename', 'skip'):
        return policy
    if not sys.stdin.isatty():
        raise FileExistsError(f'{label}. Rerun with --on-conflict rename or --on-conflict skip, or use an interactive terminal.')
    while True:
        choice = input(f'{label}. [r]ename / [s]kip / [q]uit: ').strip().lower()
        if choice in ('r', 'rename'):
            return 'rename'
        if choice in ('s', 'skip'):
            return 'skip'
        if choice in ('q', 'quit'):
            raise ValueError('Conflict left unresolved; resume this job later')


def resolve_upload_conflicts(job, remote):
    after = 0
    while rows := remote.get(f"jobs/{job['remote']}/entries?after={after}"):
        for row in rows:
            after = row['seq']
            if row['state'] != 'conflict':
                continue
            # A directory decision may already have resolved this descendant.
            current = remote.get(f"uploads/{row['id']}")
            if current['state'] != 'conflict':
                continue
            action = conflict_choice(job.get('conflict_policy', 'ask'), f"Name conflict: {row['source_path']}")
            result = remote.post(f"uploads/{row['id']}/resolve", {'action': action})
            print(f"{action}: {row['source_path']} -> {result['path']}", flush=True)


def wait_upload(remote, uid):
    delay = .025
    while True:
        current = remote.get(f'uploads/{uid}')
        if current['state'] == 'completed':
            return
        if current['state'] in ('failed', 'repair_required', 'canceled', 'paused') or current.get('job_state') in ('paused', 'canceled'):
            raise ValueError(current.get('error') or 'Transfer is paused/canceled; resume it on the server before continuing')
        time.sleep(delay)
        delay = min(.5, delay * 2)


def scan_upload(state, job, remote):
    state.db.execute('CREATE TEMP TABLE IF NOT EXISTS scan_seen(path TEXT PRIMARY KEY)')
    state.execute('DELETE FROM scan_seen')
    scan = iter(walk_source(job['source']))
    while page := list(islice(scan, 500)):
        with state.batch():
            for entry in page:
                relative_path(entry['path'])
                state.execute('INSERT INTO scan_seen VALUES (?)', (entry['path'],))
                old = state.one("SELECT * FROM entries WHERE job=? AND path=?", (job["id"], entry["path"]))
                if old and (old["size"], old["modified"], old["kind"]) != (entry["size"], entry["modified"], entry["kind"]):
                    raise ValueError("Source changed while scanning; start a new job")
                state.execute("INSERT OR IGNORE INTO entries(job,path,kind,size,modified) VALUES (?,?,?,?,?)", (job["id"], entry["path"], entry["kind"], entry["size"], entry["modified"]))
    if state.one("SELECT path FROM entries WHERE job=? AND path NOT IN (SELECT path FROM scan_seen) LIMIT 1", (job['id'],)):
        raise ValueError('Source tree lost entries since registration began. Start a new job.')
    for page in state.pages(job["id"]):
        batch, encoded_bytes = [], 0
        for entry in page:
            item = {k: entry[k] for k in ('path', 'kind', 'size', 'modified')}
            length = len(json.dumps(item).encode('utf-8')) + 1
            if batch and encoded_bytes + length > 800 * 1024:
                remote.post(f"jobs/{job['remote']}/manifest", {'allow_conflicts': True, 'entries': batch})
                batch, encoded_bytes = [], 0
            batch.append(item); encoded_bytes += length
        if batch:
            remote.post(f"jobs/{job['remote']}/manifest", {'allow_conflicts': True, 'entries': batch})
    resolve_upload_conflicts(job, remote)
    count = state.one('SELECT COUNT(*) count FROM entries WHERE job=?', (job['id'],))['count']
    remote.post(f"jobs/{job['remote']}/seal", {'expected_entries': count})
    state.execute("UPDATE jobs SET sealed=1 WHERE id=?", (job["id"],))


def upload(state, job, remote, workers=2):
    if not job["remote"]:
        registered = remote.post("jobs", {"key": job["id"], "name": Path(job["source"]).name})
        state.execute("UPDATE jobs SET remote=? WHERE id=?", (registered["id"], job["id"]))
        job["remote"] = registered["id"]
    if not job["sealed"]:
        # A seal acknowledgement may have been lost; query the server before re-registering.
        current = remote.get(f"jobs/{job['remote']}")
        if current and current["state"] != "registering":
            state.execute("UPDATE jobs SET sealed=1 WHERE id=?", (job["id"],))
        else:
            scan_upload(state, job, remote)
    status = remote.get(f"jobs/{job['remote']}")
    if status['state'] == 'paused':
        remote.post(f"jobs/{job['remote']}/resume")
    if status['state'] == 'canceled':
        raise ValueError('This upload was canceled on the server')
    def entries():
        after = 0
        while rows := remote.get(f"jobs/{job['remote']}/entries?after={after}"):
            for row in rows:
                after = row['seq']
                if row['kind'] == 'file' and row['state'] not in ('completed', 'skipped'):
                    yield row
    workers = max(1, min(workers, remote.get('capabilities')['active_files'], 4))
    pending_verification = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        source_rows = iter(entries())
        running = set()
        exhausted = False
        while running or not exhausted:
            while len(running) < workers and not exhausted:
                row = next(source_rows, None)
                if row is None:
                    exhausted = True
                else:
                    running.add(pool.submit(upload_one, row, Path(job['source']), remote))
            if not running:
                break
            done, running = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                uid = future.result()
                if uid:
                    pending_verification.append(uid)
                if len(pending_verification) >= workers * 4:
                    wait_upload(remote, pending_verification.pop(0))
    for uid in pending_verification:
        wait_upload(remote, uid)
    if remote.get(f"jobs/{job['remote']}")['state'] != 'completed':
        raise ValueError('The server batch has unfinished or failed entries; it is not complete')
    state.execute("UPDATE jobs SET state='completed' WHERE id=?", (job["id"],))



def upload_one(row, source, remote):
    if row["kind"] == "directory" or row["state"] in ("completed", "skipped"):
        return None
    if row['state'] in ('verifying', 'finalizing'):
        return row['id']
    if row["state"] in ("canceled", "repair_required"):
        raise ValueError(f"{row['path']}: {row['state']}")
    if row["state"] in ("paused", "failed"):
        row = remote.post(f"uploads/{row['id']}/resume")
    path = confined(source.parent, row.get("source_path") or row["path"], normalize=False)
    before = version(path)
    if path.stat().st_size != row["size"]:
        raise ValueError(f"Source size changed: {row['path']}")
    whole = hashlib.sha256()
    print(f"Checking source: {row['path']} ({row['offset']} committed bytes)", flush=True)
    with path.open("rb") as f:
        checked = 0
        while checked < row["offset"]:
            receipts = remote.get(f"uploads/{row['id']}/receipts?after={checked}")
            if not receipts:
                raise ValueError("Missing receipts")
            for receipt in receipts:
                if receipt["offset"] != checked or checked + receipt["length"] > row["offset"]:
                    raise ValueError("Invalid committed prefix")
                block = f.read(receipt["length"])
                if len(block) != receipt["length"] or hashlib.sha256(block).hexdigest() != receipt["sha256"]:
                    raise ValueError("Source changed: committed prefix differs. Start a new job.")
                whole.update(block)
                checked += len(block)
        offset = checked
        def prepare(length):
            block = f.read(length)
            if len(block) != length:
                raise ValueError('Source was truncated')
            return block, hashlib.sha256(block).hexdigest()
        # Exactly one next chunk may be prepared while the current request is in flight.
        with ThreadPoolExecutor(max_workers=1) as reader:
            prepared = reader.submit(prepare, min(row['chunk_size'], row['size'] - offset)) if offset < row['size'] else None
            while offset < row['size']:
                block, digest = prepared.result()
                following = offset + len(block)
                prepared = reader.submit(prepare, min(row['chunk_size'], row['size'] - following)) if following < row['size'] else None
                rid = secrets.token_hex(16)
                headers = {'Content-Type': 'application/octet-stream', 'Upload-Offset': str(offset), 'Upload-Length': str(len(block)), 'Upload-SHA256': digest, 'Upload-Request-ID': rid}
                result = remote.request('PUT', f"uploads/{row['id']}/data", content=block, headers=headers)
                if result['offset'] != following:
                    raise ValueError('Unexpected server offset; resume to reconcile')
                whole.update(block)
                offset = following
                print(f"{row['path']}: {offset}/{row['size']} committed", flush=True)
    if version(path) != before:
        raise ValueError("Source changed during upload. Start a new job.")
    remote.post(f"uploads/{row['id']}/complete", {"sha256": whole.hexdigest()})
    print(f"Verifying: {row['path']}", flush=True)
    return row['id']


def load_download_manifest(state, job, remote):
    after = 0
    # Build a disk-backed manifest. Duplicate normalized paths cannot overwrite each other.
    state.db.execute("CREATE TABLE IF NOT EXISTS path_claims(job TEXT, norm TEXT, path TEXT, PRIMARY KEY(job,norm))")
    while True:
        result = remote.get(f"shares/{safe_id(job['remote'])}/manifest?after={after}")
        if result["state"] != "completed":
            raise ValueError("Shared batch is incomplete. Wait for all files to verify before downloading the folder.")
        rows = result["entries"]
        with state.batch():
            for row in rows:
                safe_id(row["id"])
                path = relative_path(row["path"])
                if path.split('/')[0].casefold() == '.lan-transfer-parts':
                    raise ValueError("Share conflicts with the private download staging directory")
                if row["kind"] not in ("file", "directory") or type(row["size"]) is not int or row["size"] < 0:
                    raise ValueError("Invalid share manifest")
                parts = path.split('/')
                for index in range(1, len(parts) + 1):
                    prefix = '/'.join(parts[:index])
                    old = state.one("SELECT path FROM path_claims WHERE job=? AND norm=?", (job["id"], prefix.casefold()))
                    if old and old["path"] != prefix:
                        raise ValueError("Share contains case-insensitive path collisions")
                    state.execute("INSERT OR IGNORE INTO path_claims VALUES (?,?,?)", (job["id"], prefix.casefold(), prefix))
                state.execute("INSERT OR IGNORE INTO entries(job,id,path,kind,size,version,sha256) VALUES (?,?,?,?,?,?,?)", (job["id"], row["id"], path, row["kind"], row["size"], row.get("version"), row.get("sha256")))
                after = row["seq"]
        if len(rows) < 500:
            break
    state.execute("UPDATE jobs SET sealed=1 WHERE id=?", (job["id"],))


def verify_checkpoint(path, offset, expected):
    hasher = hashlib.sha256()
    if not path.exists():
        if offset:
            raise ValueError("Partial file is missing committed bytes")
        with path.open("xb"):
            pass
    if path.stat().st_size < offset:
        raise ValueError("Partial file is shorter than its checkpoint")
    with path.open("r+b") as f:
        remaining = offset
        while remaining:
            block = f.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError('Partial file became shorter while validating its checkpoint')
            hasher.update(block)
            remaining -= len(block)
        if offset and hasher.hexdigest() != expected:
            raise ValueError("Partial file checkpoint checksum mismatch; preserve this job and start a fresh download")
        f.truncate(offset)
        f.flush()
        os.fsync(f.fileno())
    fsync_dir(path.parent)
    return hasher


def validate_range(response, start, end, size, pinned):
    if response.status_code == 200:
        raise ValueError("Server returned a full response to a range request; no bytes appended. Source version may have changed.")
    if response.status_code == 416:
        raise ValueError("Server rejected the checkpoint range; no completion inferred. Check the source version and restart the job.")
    response.raise_for_status()
    if response.status_code != 206 or response.headers.get("content-range") != f"bytes {start}-{end}/{size}" or response.headers.get("etag") != f'"{pinned}"' or response.headers.get("content-length") != str(end - start + 1):
        raise ValueError("Invalid range response, length, or pinned version; no bytes appended")


def download_destination(state, job, row):
    root = Path(job['destination'])
    relative = row.get('destination_path') or row['path']
    destination = confined(root, relative, mkdir=True)
    if row['state'] == 'skipped':
        return None
    if destination.exists() and row['state'] not in ('completed', 'finalizing') and not (row['kind'] == 'directory' and destination.is_dir()):
        if row.get('destination_path'):
            raise FileExistsError(f'The saved destination is now occupied: {destination}. Move that unrelated file before resuming.')
        action = conflict_choice(job.get('conflict_policy', 'ask'), f'Destination exists: {destination}')
        if action == 'skip':
            state.execute("UPDATE entries SET state='skipped' WHERE seq=?", (row['seq'],))
            if row['kind'] == 'directory':
                state.execute("UPDATE entries SET state='skipped' WHERE job=? AND path>=? AND path<?", (job['id'], row['path'] + '/', row['path'] + '0'))
            print(f"Skipped destination: {relative}", flush=True)
            return None
        original = Path(relative)
        suffix = original.suffix if row['kind'] == 'file' else ''
        stem = original.name[:-len(suffix)] if suffix else original.name
        for number in range(2, 10002):
            candidate = (original.parent / f'{stem} ({number}){suffix}').as_posix()
            claimed = state.one('SELECT path FROM path_claims WHERE job=? AND norm=?', (job['id'], candidate.casefold()))
            if not claimed and not confined(root, candidate).exists():
                relative = candidate
                destination = confined(root, relative, mkdir=True)
                break
        else:
            raise FileExistsError('No unique destination name available')
        state.execute('INSERT INTO path_claims VALUES (?,?,?)', (job['id'], relative.casefold(), relative))
        print(f"Renamed destination: {row['path']} -> {relative}", flush=True)
    state.execute('UPDATE entries SET destination_path=? WHERE seq=?', (relative, row['seq']))
    if row['kind'] == 'directory' and relative != row['path']:
        # Persist the subtree mapping before creating/writing any destination bytes.
        state.execute("UPDATE entries SET destination_path=? || substr(path,?) WHERE job=? AND path>=? AND path<? AND state='queued'", (relative, len(row['path']) + 1, job['id'], row['path'] + '/', row['path'] + '0'))
    return destination


def download_file(state, job, row, remote):
    # Refresh state: an earlier directory decision can rename or skip this descendant.
    row = state.one('SELECT * FROM entries WHERE seq=?', (row['seq'],))
    if row['state'] == 'skipped':
        return
    root = Path(job["destination"])
    destination = download_destination(state, job, row)
    if destination is None:
        return
    if row["kind"] == "directory":
        destination.mkdir(exist_ok=True)
        fsync_dir(destination.parent)
        state.execute("UPDATE entries SET state='completed' WHERE seq=?", (row['seq'],))
        return
    check_file_size(row['size'], filesystem_name(destination.parent))
    metadata = remote.get(f"files/{safe_id(row['id'])}/metadata")
    if metadata["version"] != row["version"] or metadata["size"] != row["size"] or (row["sha256"] and metadata["sha256"] != row["sha256"]):
        raise ValueError("Source version changed. Register a new share and download job.")
    expected = metadata["sha256"]
    if not re.fullmatch('[a-f0-9]{64}', expected or ''):
        raise ValueError("Server has no valid source checksum")
    state.execute("UPDATE entries SET sha256=? WHERE seq=?", (expected, row["seq"]))
    if row["state"] in ("completed", "finalizing") and destination.exists():
        if destination.stat().st_size == row["size"] and digest_file(destination) == expected:
            state.execute("UPDATE entries SET state='completed' WHERE seq=?", (row["seq"],))
            return
        raise ValueError(f"Completed destination changed: {destination}")
    if destination.exists():
        raise FileExistsError(f"Destination exists: {destination}. Choose a different --to folder; existing files are never overwritten.")
    staging = confined(root, f".lan-transfer-parts/{safe_id(job['id'])}/{safe_id(row['id'])}.part", mkdir=True)
    offset = row["offset"]
    if not 0 <= offset <= row['size']:
        raise ValueError('Checkpoint offset is outside the pinned source size')
    whole = verify_checkpoint(staging, offset, row["prefix"])
    attempt = 0
    while offset < row['size']:
        start, end = offset, row['size'] - 1
        trial = whole.copy()
        try:
            with remote.client.stream('GET', f"files/{row['id']}/content", headers={'Range': f'bytes={start}-{end}', 'If-Range': f'"{row["version"]}"'}) as response:
                validate_range(response, start, end, row['size'], row['version'])
                position = start
                checkpoint = min(start + BLOCK, row['size'])
                with staging.open('r+b') as f:
                    f.seek(start)
                    for block in response.iter_bytes(1024 * 1024):
                        if position + len(block) > row['size']:
                            raise ValueError('Oversized response')
                        f.write(block)
                        trial.update(block)
                        position += len(block)
                        if position >= checkpoint:
                            f.flush()
                            os.fsync(f.fileno())
                            state.execute('UPDATE entries SET offset=?,prefix=? WHERE seq=?', (position, trial.hexdigest(), row['seq']))
                            offset = position
                            whole = trial.copy()
                            checkpoint = min(offset + BLOCK, row['size'])
                            attempt = 0
                            print(f"{row['path']}: {offset}/{row['size']} checkpointed", flush=True)
                    if position != row['size']:
                        raise httpx.ReadError('Truncated range response')
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            # The live hasher already represents this durable prefix. Only a new process
            # needs to reread it; truncating the uncertain suffix keeps live retries cheap.
            with staging.open('r+b') as f:
                if f.seek(0, os.SEEK_END) < offset:
                    raise ValueError('Partial file is shorter than its checkpoint') from exc
                f.truncate(offset)
                f.flush()
                os.fsync(f.fileno())
            if attempt >= 8 or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (408, 429, 500, 502, 503, 504)):
                raise
            retry_after = float(exc.response.headers.get('retry-after', 0)) if isinstance(exc, httpx.HTTPStatusError) else 0
            time.sleep(max(retry_after, min(30, 2**attempt) * random.uniform(.75, 1.25)))
            attempt += 1
    print(f"Verifying destination: {row['path']}", flush=True)
    if digest_file(staging) != expected:
        raise ValueError("Downloaded bytes failed whole-file SHA-256 verification")
    state.execute("UPDATE entries SET state='finalizing' WHERE seq=?", (row["seq"],))
    publish(staging, destination)
    state.execute("UPDATE entries SET state='completed' WHERE seq=?", (row["seq"],))


def download(state, job, remote):
    root = Path(job["destination"])
    if any(is_link(p) for p in [root, *root.parents]):
        raise ValueError("Download destination must not contain symlinks/junctions")
    root.mkdir(parents=True, exist_ok=True)
    if not job["sealed"]:
        load_download_manifest(state, job, remote)
    for page in state.pages(job["id"]):
        for row in page:
            download_file(state, job, row, remote)
    if state.one("SELECT seq FROM entries WHERE job=? AND state NOT IN ('completed','skipped') LIMIT 1", (job['id'],)):
        raise ValueError('Download still has unfinished entries')
    state.execute("UPDATE jobs SET state='completed' WHERE id=?", (job["id"],))
