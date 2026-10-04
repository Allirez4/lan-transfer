import asyncio
import errno
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import threading
import time
import sqlite3
import logging
from functools import partial

import anyio
from fastapi import HTTPException

from .repository import Repository
from .storage import ProcessLock, check_file_size, confined, digest_file, filesystem_name, fsync_dir, is_link, publish, relative_path, version


class Service:
    def __init__(self, config):
        self.config = config
        config.storage.mkdir(parents=True, exist_ok=True)
        if any(is_link(p) for p in [config.storage, *config.storage.parents]):
            raise ValueError("Storage must not contain a symlink/junction")
        self.process_lock = ProcessLock(config.storage / "server.lock")
        self.filesystem = filesystem_name(config.storage)
        self.stage = config.storage / ".staging"
        self.received = config.storage / "received"
        for directory in (self.stage, self.received):
            if is_link(directory):
                self.process_lock.close()
                raise ValueError("Storage subdirectories must not be links")
            directory.mkdir(exist_ok=True, mode=0o700)
        self.repo = Repository(config.storage / "state.sqlite3")
        self.repo.execute("INSERT OR IGNORE INTO settings VALUES ('identity',?)", (secrets.token_hex(16),))
        self.active = anyio.Semaphore(config.active_files)
        # Downloads cannot occupy every receiving slot for the lifetime of a large file.
        self.downloads = anyio.Semaphore(config.active_files)
        self.disk_workers = anyio.CapacityLimiter(config.active_files)
        self.metadata_workers = anyio.CapacityLimiter(1)
        self.verifier_workers = anyio.CapacityLimiter(1)
        self.verification_error = None
        self.maintenance_error = None
        self.on_shutdown = None
        self.hash_lock = threading.Lock()
        self.verification_ready = asyncio.Event()
        # Fixed stripes bound lock memory even for manifests containing millions of files.
        self.locks = [anyio.Lock() for _ in range(256)]

    def lock(self, uid):
        return self.locks[int(hashlib.sha256(uid.encode()).hexdigest()[:8], 16) % len(self.locks)]

    async def disk(self, fn, *args):
        return await anyio.to_thread.run_sync(partial(fn, *args), limiter=self.disk_workers)

    def part(self, uid):
        return confined(self.stage, uid + ".part")

    def upload(self, uid, owner=None):
        row = self.repo.one("SELECT u.*,j.owner,j.state job_state FROM uploads u JOIN jobs j ON j.id=u.job WHERE u.id=?", (uid,))
        if not row:
            raise HTTPException(404, "Unknown upload")
        if owner and row["owner"] != owner:
            raise HTTPException(403, "This upload belongs to another session")
        return row

    def job(self, jid, owner):
        row = self.repo.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not row:
            raise HTTPException(404, "Unknown job")
        if row["owner"] != owner:
            raise HTTPException(403, "This job belongs to another session")
        return row

    def create_job(self, owner, key, name, expected_entries=None):
        with self.repo.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE owner=? AND key=?", (owner, key)).fetchone()
            if row:
                if row["name"] != name or row["expected_entries"] != expected_entries:
                    raise HTTPException(409, "Idempotency key already has different metadata")
                return dict(row)
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE state NOT IN ('completed','canceled')").fetchone()[0]
            if count >= self.config.max_jobs:
                raise HTTPException(429, "Too many open jobs; complete or cancel an existing job")
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE owner=? AND state NOT IN ('completed','canceled')", (owner,)).fetchone()[0]
            if count >= self.config.max_jobs_per_session:
                raise HTTPException(429, 'This device has too many open jobs')
            if expected_entries is not None and expected_entries > self.config.max_entries_per_job:
                raise HTTPException(413, 'Batch exceeds the configured entry limit')
            jid = secrets.token_hex(16)
            db.execute("INSERT INTO jobs(id,owner,key,name,created,expected_entries) VALUES (?,?,?,?,?,?)", (jid, owner, key, name, time.time(), expected_entries))
        return self.job(jid, owner)

    def disk_status(self):
        usage = shutil.disk_usage(self.config.storage)
        reserved = self.repo.one("SELECT COALESCE(SUM(u.size-u.offset),0) bytes FROM uploads u JOIN jobs j ON j.id=u.job WHERE u.state NOT IN ('completed','canceled','skipped') AND j.state!='canceled'")["bytes"]
        return {"free": usage.free, "total": usage.total, "reserved": reserved, "safety": self.config.safety_bytes, "filesystem": self.filesystem,
                "available": max(0, usage.free - reserved - self.config.safety_bytes)}

    def register(self, jid, owner, entries, allow_conflicts=False):
        from .manifests import conflict, mapped_path
        self.job(jid, owner)
        created, page_keys = [], set()
        try:
            with self.repo.transaction() as db:
                if db.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()[0] != "registering":
                    raise HTTPException(409, "Manifest is sealed")
                job_count = db.execute('SELECT COUNT(*) FROM uploads WHERE job=?', (jid,)).fetchone()[0]
                session_count = db.execute('SELECT COUNT(*) FROM uploads u JOIN jobs j ON j.id=u.job WHERE j.owner=?', (owner,)).fetchone()[0]
                for entry in entries:
                    check_file_size(entry.size, self.filesystem)
                    source = entry.path
                    relative_path(source)
                    key = entry.key or source
                    if key in page_keys:
                        raise HTTPException(409, f"Duplicate source key in manifest page: {key}")
                    page_keys.add(key)
                    existing = db.execute("SELECT * FROM uploads WHERE job=? AND source_key=?", (jid, key)).fetchone()
                    if existing:
                        if (existing["source_path"], existing["kind"], existing["size"], existing["modified"], existing['sha256']) == (source, entry.kind, entry.size, entry.modified, entry.sha256):
                            continue
                        raise HTTPException(409, f"Source key already has different metadata: {source}")
                    job_count += 1
                    session_count += 1
                    if job_count > self.config.max_entries_per_job or session_count > self.config.max_entries_per_session:
                        raise HTTPException(413, 'Manifest entry quota exceeded; contact the laptop owner')
                    output = mapped_path(db, jid, source)
                    issue = conflict(db, jid, output, entry.kind)
                    if issue and not allow_conflicts:
                        raise HTTPException(409, issue['reason'])
                    state, reason = ('conflict', issue['reason']) if issue else ('queued', None)
                    parts = source.split('/')
                    for count in range(1, len(parts)):
                        parent = db.execute("SELECT state FROM uploads WHERE job=? AND source_path=? AND kind='directory'", (jid, '/'.join(parts[:count]))).fetchone()
                        if parent and parent['state'] == 'skipped':
                            state, reason = 'skipped', 'Parent directory skipped by user'
                    uid = secrets.token_hex(16)
                    db.execute("INSERT INTO uploads(id,job,path,norm,kind,size,modified,chunk_size,source_key,source_path,state,error,sha256,resolution) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (uid, jid, output, output.casefold() if state == 'queued' else None, entry.kind, entry.size, entry.modified, self.config.chunk_size, key, source, state, reason, entry.sha256, 'skip' if state == 'skipped' else 'original'))
                    if entry.kind == "file":
                        with self.part(uid).open("xb") as f:
                            created.append(self.part(uid))
                            f.flush()
                            os.fsync(f.fileno())
                disk = self.disk_status()
                if disk['free'] < disk['reserved'] + self.config.safety_bytes:
                    raise HTTPException(507, "Not enough disk space for admitted files plus the safety margin")
                fsync_dir(self.stage)
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        return {"registered": len(entries), "conflicts": self.repo.one("SELECT COUNT(*) count FROM uploads WHERE job=? AND state='conflict'", (jid,))['count']}

    def seal(self, jid, owner, expected_entries=None):
        with self.repo.lock:
            row = self.job(jid, owner)
            if row["state"] != "registering":
                return row
            expected = row['expected_entries'] if row['expected_entries'] is not None else expected_entries
            actual = self.repo.one("SELECT COUNT(*) count FROM uploads WHERE job=?", (jid,))['count']
            if expected is not None and actual != expected:
                raise HTTPException(409, f"Manifest is incomplete: registered {actual} of {expected} entries. Reselect the full original source.")
            if self.repo.one("SELECT id FROM uploads WHERE job=? AND state='conflict' LIMIT 1", (jid,)):
                raise HTTPException(409, "Resolve or explicitly skip conflicting names before starting")
            root = confined(self.received, jid)
            root.mkdir(exist_ok=True)
            after = 0
            while entries := self.repo.all("SELECT * FROM uploads WHERE job=? AND kind='directory' AND state!='skipped' AND seq>? ORDER BY seq LIMIT 500", (jid, after)):
                for entry in entries:
                    confined(root, entry["path"], mkdir=True).mkdir(exist_ok=True)
                    after = entry["seq"]
            with self.repo.transaction() as db:
                db.execute("UPDATE uploads SET state='completed' WHERE job=? AND kind='directory' AND state!='skipped'", (jid,))
                db.execute("INSERT OR IGNORE INTO shares VALUES (?,?,?,?)", (jid, row["name"], str(root), time.time()))
                db.execute("INSERT OR IGNORE INTO files(id,share,path,kind,size) SELECT id,job,path,kind,size FROM uploads WHERE job=? AND kind='directory' AND state='completed'", (jid,))
                db.execute("UPDATE jobs SET state='queued' WHERE id=?", (jid,))
            self.refresh_job(jid)
            return self.job(jid, owner)

    def refresh_job(self, jid):
        with self.repo.transaction() as db:
            job = db.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()
            if not job or job[0] in ("registering", "paused", "canceled"):
                return
            counts = {r[0]: r[1] for r in db.execute("SELECT state,COUNT(*) FROM uploads WHERE job=? GROUP BY state", (jid,))}
            if any(counts.get(s) for s in ("failed", "repair_required", "canceled")):
                state = "failed"
            elif any(counts.get(s) for s in ("verifying", "finalizing")):
                state = "verifying"
            elif sum(counts.values()) == counts.get("completed", 0) + counts.get("skipped", 0):
                state = "completed"
            else:
                state = "queued"
            db.execute("UPDATE jobs SET state=? WHERE id=?", (state, jid))

    def rollback(self, path, offset):
        with path.open("r+b") as f:
            f.truncate(offset)
            f.flush()
            os.fsync(f.fileno())

    async def append(self, uid, owner, request, offset, length, sha256, request_id):
        async with self.active, self.lock(uid):
            row = await self.disk(self.upload, uid, owner)
            receipt = await self.disk(self.repo.one, "SELECT * FROM receipts WHERE upload=? AND request_id=?", (uid, request_id))
            if receipt:
                if (receipt["offset"], receipt["length"], receipt["sha256"]) != (offset, length, sha256):
                    raise HTTPException(409, "Request ID was already used for different bytes")
                return {"offset": row["offset"], "replayed": True}
            if row["state"] not in ("queued", "uploading") or row["job_state"] not in ("queued", "verifying", "failed"):
                raise HTTPException(409, f"Upload is {row['state']}; job is {row['job_state']}")
            if row["kind"] != "file" or offset != row["offset"]:
                raise HTTPException(409, "Offset conflict; query authoritative status")
            if length <= 0 or length != min(row["chunk_size"], row["size"] - offset):
                raise HTTPException(422, "Invalid chunk length")
            if shutil.disk_usage(self.config.storage).free < length + self.config.safety_bytes:
                raise HTTPException(507, "Disk space is low. Free space and resume.")
            path = self.part(uid)
            if not path.exists() or path.stat().st_size < offset:
                await self.disk(self.repo.execute, "UPDATE uploads SET state='repair_required',error='Staging file is missing committed bytes; restore storage or cancel and start again' WHERE id=?", (uid,))
                await self.disk(self.refresh_job, row['job'])
                raise HTTPException(409, 'Staging file is missing committed bytes; cancel and start again or restore storage')
            f = await self.disk(path.open, "r+b")
            hasher = hashlib.sha256()
            received = 0
            committed = False
            def write(block):
                f.write(block)
                hasher.update(block)
            def durable_commit():
                nonlocal committed
                f.flush()
                os.fsync(f.fileno())
                with self.repo.transaction() as db:
                    db.execute("INSERT INTO receipts VALUES (?,?,?,?,?)", (uid, offset, length, sha256, request_id))
                    db.execute("UPDATE uploads SET offset=?,state='uploading',error=NULL WHERE id=?", (offset + length, uid))
                committed = True
            try:
                await self.disk(f.seek, offset)
                with anyio.fail_after(120):
                    async for block in request.stream():
                        received += len(block)
                        if received > length:
                            raise HTTPException(422, "Chunk body exceeds declared length")
                        for start in range(0, len(block), 1024 * 1024):
                            await self.disk(write, block[start:start + 1024 * 1024])
                if received != length or hasher.hexdigest() != sha256:
                    raise HTTPException(422, "Incomplete chunk or SHA-256 mismatch")
                # Once fsync begins, shield the entire DB commit/ack boundary from cancellation.
                with anyio.CancelScope(shield=True):
                    await self.disk(durable_commit)
            except OSError as exc:
                message = "Disk full; free space and resume" if exc.errno in (errno.ENOSPC, errno.EDQUOT) else "Storage write failed; check disk permissions/health and resume"
                raise HTTPException(507, message) from exc
            except TimeoutError:
                raise HTTPException(408, "Chunk timed out; reconcile offset and retry") from None
            finally:
                with anyio.CancelScope(shield=True):
                    try:
                        await self.disk(f.close)
                    finally:
                        if not committed:
                            try:
                                await self.disk(self.rollback, path, offset)
                            except OSError:
                                await self.disk(self.repo.execute, "UPDATE uploads SET state='repair_required',error='Rollback failed; check storage before recovery' WHERE id=?", (uid,))
            return {"offset": offset + length, "replayed": False}

    def queue_verification(self, uid, owner, sha256):
        row = self.upload(uid, owner)
        if row["sha256"] and row["sha256"] != sha256:
            raise HTTPException(409, "Final digest conflicts with the registered source")
        if row["state"] in ("completed", "verifying", "finalizing"):
            return row
        if row["offset"] != row["size"] or row["kind"] != "file" or row["state"] in ("canceled", "skipped", "conflict", "repair_required", "paused") or row["job_state"] in ("registering", "paused", "canceled"):
            raise HTTPException(409, "Upload is not ready to finalize")
        self.repo.execute("UPDATE uploads SET sha256=?,state='verifying',error=NULL WHERE id=?", (sha256, uid))
        self.refresh_job(row["job"])
        return self.upload(uid, owner)

    def verify(self, uid):
        row = self.upload(uid)
        try:
            path = self.part(uid)
            destination = confined(confined(self.received, row["job"]), row["path"], mkdir=True)
            if row["state"] == "finalizing" and destination.exists():
                # An existing target can only be adopted after strict size+hash reconciliation.
                if destination.stat().st_size != row["size"] or digest_file(destination) != row["sha256"]:
                    raise ValueError("Publication conflict: destination differs from verified source")
                if path.exists():
                    if not os.path.samefile(path, destination):
                        raise ValueError("Publication conflict: unrelated destination already exists")
                    path.unlink()
            else:
                if path.stat().st_size != row["size"] or digest_file(path) != row["sha256"]:
                    raise ValueError("Whole-file SHA-256 mismatch; cancel and reselect an unchanged source")
                self.repo.execute("UPDATE uploads SET state='finalizing' WHERE id=?", (uid,))
                publish(path, destination)
            with self.repo.transaction() as db:
                db.execute("INSERT OR IGNORE INTO files(id,share,path,kind,size,version,sha256) VALUES (?,?,?,'file',?,?,?)",
                           (uid, row["job"], row["path"], row["size"], version(destination), row["sha256"]))
                db.execute("UPDATE uploads SET state='completed',error=NULL WHERE id=?", (uid,))
        except (OSError, ValueError) as exc:
            self.repo.execute("UPDATE uploads SET state='failed',error=? WHERE id=?", (str(exc), uid))
        self.refresh_job(row["job"])

    def recover(self):
        # Keyset pagination keeps restart memory independent of manifest size.
        after = 0
        while rows := self.repo.all("SELECT * FROM uploads WHERE seq>? ORDER BY seq LIMIT 500", (after,)):
            for row in rows:
                after = row["seq"]
                if row["kind"] == "directory" or row["state"] in ("completed", "canceled", "skipped", "finalizing"):
                    continue
                path = self.part(row["id"])
                if not path.exists() or path.stat().st_size < row["offset"]:
                    self.repo.execute("UPDATE uploads SET state='repair_required',error='Staging file is missing committed bytes; restore storage or cancel and start again' WHERE id=?", (row["id"],))
                elif path.stat().st_size > row["offset"]:
                    self.rollback(path, row["offset"])
                self.refresh_job(row["job"])
        # Completion and its job summary may straddle a failed summary transaction.
        for job in self.repo.all("SELECT id FROM jobs WHERE state NOT IN ('registering','paused','canceled','completed')"):
            self.refresh_job(job['id'])

    async def verification_loop(self):
        delay = .25
        pending_refresh = None
        while True:
            row = None
            try:
                if pending_refresh:
                    await self.disk(self.refresh_job, pending_refresh)
                    pending_refresh = None
                row = await self.disk(self.repo.one, "SELECT u.id,u.job FROM uploads u JOIN jobs j ON j.id=u.job WHERE u.state IN ('verifying','finalizing') AND j.state NOT IN ('paused','canceled') ORDER BY u.seq LIMIT 1")
                if row:
                    async with self.lock(row['id']):
                        latest = await self.disk(self.upload, row['id'])
                        if latest['state'] in ('verifying', 'finalizing') and latest['job_state'] not in ('paused', 'canceled'):
                            await anyio.to_thread.run_sync(self.verify, row['id'], limiter=self.verifier_workers)
                else:
                    with anyio.move_on_after(.2):
                        await self.verification_ready.wait()
                    self.verification_ready.clear()
                self.verification_error = None
                delay = .25
            except Exception as exc:
                # Keep durable verifying/finalizing state so a retry can reconcile a rename
                # whose final database commit failed. Never close the live service here.
                self.verification_error = str(exc)
                if row:
                    pending_refresh = row['job']
                logging.getLogger(__name__).exception('Verification will retry')
                await anyio.sleep(delay)
                delay = min(5, delay * 2)

    async def control_job(self, jid, action, owner=None):
        row = await self.disk(self.repo.one, 'SELECT * FROM jobs WHERE id=?', (jid,))
        if not row:
            raise HTTPException(404, 'Unknown job')
        if owner and row['owner'] != owner:
            raise HTTPException(403, 'This job belongs to another session')
        if action == 'cancel' and row['state'] == 'canceled':
            return row
        if row['state'] in ('completed', 'canceled') or (row['state'] == 'registering' and action != 'cancel'):
            raise HTTPException(409, 'Job cannot change state now')
        state = {'pause': 'paused', 'resume': 'queued', 'cancel': 'canceled'}[action]
        await self.disk(self.repo.execute, 'UPDATE jobs SET state=? WHERE id=?', (state, jid))
        if action == 'cancel':
            after = 0
            while rows := await self.disk(self.repo.all, 'SELECT id,seq FROM uploads WHERE job=? AND seq>? ORDER BY seq LIMIT 500', (jid, after)):
                for entry in rows:
                    after = entry['seq']
                    async with self.lock(entry['id']):
                        await self.disk(self.repo.execute, "UPDATE uploads SET state='canceled' WHERE id=? AND state NOT IN ('completed','skipped')", (entry['id'],))
        await self.disk(self.refresh_job, jid)
        return await self.disk(self.repo.one, 'SELECT * FROM jobs WHERE id=?', (jid,))

    async def discard_incomplete(self, jid, owner=None):
        row = await self.disk(self.repo.one, 'SELECT * FROM jobs WHERE id=?', (jid,))
        if not row:
            raise HTTPException(404, 'Unknown job')
        if owner and row['owner'] != owner:
            raise HTTPException(403, 'This job belongs to another session')
        if row['state'] != 'canceled':
            raise HTTPException(409, 'Cancel the batch before discarding incomplete data')
        after = 0
        while rows := await self.disk(self.repo.all, "SELECT id,seq FROM uploads WHERE job=? AND kind='file' AND state!='completed' AND seq>? ORDER BY seq LIMIT 500", (jid, after)):
            for entry in rows:
                after = entry['seq']
                async with self.lock(entry['id']):
                    latest = await self.disk(self.upload, entry['id'])
                    if latest['state'] != 'completed':
                        await self.disk(self.part(entry['id']).unlink, True)
        await self.disk(fsync_dir, self.stage)
        await self.disk(self.repo.execute, 'UPDATE jobs SET discarded=1 WHERE id=?', (jid,))
        return await self.disk(self.repo.one, 'SELECT * FROM jobs WHERE id=?', (jid,))

    async def revoke_session(self, sid):
        await self.disk(self.repo.execute, 'UPDATE sessions SET revoked=1 WHERE id=?', (sid,))
        for job in await self.disk(self.repo.all, "SELECT id FROM jobs WHERE owner=? AND state NOT IN ('completed','canceled')", (sid,)):
            try:
                await self.control_job(job['id'], 'cancel')
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise

    async def cleanup_orphans(self):
        rows = await self.disk(self.repo.all, "SELECT DISTINCT s.id FROM sessions s JOIN jobs j ON j.owner=s.id WHERE (s.revoked=1 OR s.created<=?) AND j.state NOT IN ('completed','canceled')", (time.time() - self.config.session_days * 86400,))
        for row in rows:
            await self.revoke_session(row['id'])

    async def maintenance_loop(self):
        from .admin import process_commands
        while True:
            try:
                await self.cleanup_orphans()
                await process_commands(self)
                self.maintenance_error = None
            except Exception as exc:
                self.maintenance_error = str(exc)
                logging.getLogger(__name__).exception('Local maintenance will retry')
            await anyio.sleep(1)

    def share(self, sid):
        from .shares import allowed_share
        row = self.repo.one('SELECT * FROM shares WHERE id=?', (sid,))
        if not row or not allowed_share(self.config, self.repo, row):
            raise HTTPException(404, 'Share is no longer published')
        return row

    def list_shares(self, after=0, after_id='', limit=500):
        from .shares import allowed_share
        result = []
        while rows := self.repo.all("SELECT s.*,COUNT(f.id) count,COALESCE(SUM(f.size),0) size FROM shares s LEFT JOIN files f ON f.share=s.id WHERE s.created>? OR (?!='' AND s.created=? AND s.id>?) GROUP BY s.id ORDER BY s.created,s.id LIMIT 500", (after, after_id, after, after_id)):
            for row in rows:
                after, after_id = row['created'], row['id']
                if allowed_share(self.config, self.repo, row):
                    row.pop('root')
                    result.append(row)
                    if len(result) == limit:
                        return result
        return result

    def file(self, fid):
        row = self.repo.one("SELECT f.*,s.root FROM files f JOIN shares s ON s.id=f.share WHERE f.id=?", (fid,))
        if not row or row["kind"] != "file":
            raise HTTPException(404, "Unknown file")
        self.share(row['share'])
        path = confined(Path(row["root"]), row["path"], normalize=False)
        if not path.exists() or version(path) != row["version"]:
            raise HTTPException(409, "Shared source changed; register a new share before downloading")
        return row, path

    def metadata(self, fid):
        row, path = self.file(fid)
        if not row["sha256"]:
            with self.hash_lock:
                cached = self.repo.one("SELECT sha256 FROM files WHERE id=?", (fid,))
                digest = cached['sha256'] if cached and cached['sha256'] else digest_file(path)
                if version(path) != row["version"]:
                    raise HTTPException(409, "Source changed while hashing")
                self.repo.execute("UPDATE files SET sha256=? WHERE id=?", (digest, fid))
                row["sha256"] = digest
        row.pop("root", None)
        return row

    def close(self):
        self.repo.close()
        self.process_lock.close()
