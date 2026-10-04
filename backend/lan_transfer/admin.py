"""Administrative commands use the local database, never a network admin API."""
import secrets
import time

import anyio
from fastapi import HTTPException

from .repository import Repository
from .storage import is_link


def open_repository(config):
    if any(is_link(p) for p in [config.storage, *config.storage.parents]):
        raise ValueError('Storage must not contain symlinks/junctions')
    config.storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Repository(config.storage / 'state.sqlite3')


async def process_commands(service):
    rows = await service.disk(service.repo.all, "SELECT * FROM admin_commands WHERE state IN ('queued','running') ORDER BY created LIMIT 20")
    for row in rows:
        await service.disk(service.repo.execute, "UPDATE admin_commands SET state='running',error=NULL WHERE id=?", (row['id'],))
        try:
            if row['action'] == 'revoke':
                await service.revoke_session(row['target'])
            elif row['action'] in ('pause', 'resume', 'cancel'):
                job = await service.disk(service.repo.one, 'SELECT state FROM jobs WHERE id=?', (row['target'],))
                desired = {'pause': 'paused', 'resume': 'queued', 'cancel': 'canceled'}[row['action']]
                if not job or job['state'] != desired:
                    await service.control_job(row['target'], row['action'])
            elif row['action'] == 'discard':
                await service.discard_incomplete(row['target'])
            elif row['action'] == 'stop':
                if not service.on_shutdown:
                    raise ValueError('This server must be stopped by its launcher')
                service.on_shutdown()
            else:
                raise ValueError('Unknown local command')
        except (HTTPException, ValueError) as exc:
            await service.disk(service.repo.execute, "UPDATE admin_commands SET state='failed',error=? WHERE id=?",
                               (str(exc.detail) if isinstance(exc, HTTPException) else str(exc), row['id']))
        else:
            await service.disk(service.repo.execute, "UPDATE admin_commands SET state='completed' WHERE id=?", (row['id'],))


def submit(config, action, target=''):
    if action not in ('revoke', 'pause', 'resume', 'cancel', 'discard', 'stop'):
        raise ValueError('Unknown local command')
    repo = open_repository(config)
    cid = secrets.token_hex(16)
    try:
        # Stop accepting this credential immediately, even if a chunk is completing.
        if action == 'revoke':
            repo.execute('UPDATE sessions SET revoked=1 WHERE id=?', (target,))
        repo.execute('INSERT INTO admin_commands(id,action,target,created) VALUES (?,?,?,?)', (cid, action, target, time.time()))
    finally:
        repo.close()

    async def when_offline():
        from .service import Service
        try:
            service = Service(config)
        except RuntimeError as exc:
            if 'already using' not in str(exc):
                raise
            return  # The live server will process this durable command with its file locks.
        try:
            await service.cleanup_orphans()
            await process_commands(service)
        finally:
            service.close()
    anyio.run(when_offline)
    return cid
