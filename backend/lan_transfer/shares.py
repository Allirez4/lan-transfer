"""Local-only share registration. No HTTP endpoint accepts filesystem paths."""
import os
from pathlib import Path
import secrets
import time

from .storage import confined, is_link, relative_path, version


def within_roots(config, source):
    return any(Path(source).resolve().is_relative_to(Path(root).resolve()) for root in config.share_roots)


def allowed_share(config, repo, share):
    policy = repo.one('SELECT * FROM share_policy WHERE share=?', (share['id'],))
    if policy:
        return bool(policy['enabled']) and (policy['approval'] != 'configured' or within_roots(config, policy['source']))
    if repo.one('SELECT id FROM jobs WHERE id=?', (share['id'],)):
        return True  # Received batch, including databases created before share policies.
    first = repo.one('SELECT path FROM files WHERE share=? ORDER BY seq LIMIT 1', (share['id'],))
    return bool(first) and within_roots(config, Path(share['root']) / first['path'].split('/')[0])


def register_share(config, repo, source, *, local_selection=False, share_id=None):
    source = Path(source).absolute()
    if any(is_link(p) for p in [source, *source.parents]):
        raise ValueError('Share paths must not contain symlinks or junctions')
    if not local_selection and not within_roots(config, source):
        raise ValueError('Path is outside locally configured share_roots')
    if not source.is_file() and not source.is_dir():
        raise ValueError('Share source does not exist or is not an ordinary file/folder')
    sid = share_id or secrets.token_hex(16)
    with repo.transaction() as db:
        if share_id:
            if not db.execute('SELECT id FROM shares WHERE id=?', (sid,)).fetchone():
                raise ValueError('Unknown share')
            if db.execute('SELECT id FROM jobs WHERE id=?', (sid,)).fetchone():
                raise ValueError('Received batches cannot be refreshed from a source')
            db.execute('DELETE FROM files WHERE share=?', (sid,))
            db.execute('UPDATE shares SET name=?,root=? WHERE id=?', (source.name, str(source.parent), sid))
        else:
            db.execute('INSERT INTO shares VALUES (?,?,?,?)', (sid, source.name, str(source.parent), time.time()))
        db.execute('INSERT OR REPLACE INTO share_policy VALUES (?,?,?,1)',
                   (sid, str(source), 'selected' if local_selection else 'configured'))
        db.execute('CREATE TEMP TABLE IF NOT EXISTS share_claims(norm TEXT PRIMARY KEY)')
        db.execute('DELETE FROM share_claims')
        count = 0

        def add(path, kind):
            nonlocal count
            count += 1
            if count > config.max_entries_per_job:
                raise ValueError('Share exceeds the configured entry limit')
            relative = path.relative_to(source.parent).as_posix()
            normalized = relative_path(relative)
            confined(source.parent, relative, normalize=False)
            if db.execute('SELECT norm FROM share_claims WHERE norm=?', (normalized.casefold(),)).fetchone():
                raise ValueError('Share contains normalized/case-insensitive collisions')
            db.execute('INSERT INTO share_claims VALUES (?)', (normalized.casefold(),))
            size, pinned = (path.stat().st_size, version(path)) if kind == 'file' else (0, None)
            db.execute('INSERT INTO files(id,share,path,kind,size,version) VALUES (?,?,?,?,?,?)',
                       (secrets.token_hex(16), sid, relative, kind, size, pinned))

        if source.is_file():
            add(source, 'file')
        else:
            def walk_error(error):
                raise error
            for base, dirs, files in os.walk(source, followlinks=False, onerror=walk_error):
                base = Path(base)
                add(base, 'directory')
                for name in [*dirs, *files]:
                    if is_link(base / name):
                        raise ValueError('Symlinks/junctions are not supported in shared folders')
                for name in files:
                    add(base / name, 'file')
    return sid


def unshare(repo, sid):
    with repo.transaction() as db:
        if not db.execute('SELECT id FROM shares WHERE id=?', (sid,)).fetchone():
            raise ValueError('Unknown share')
        db.execute("INSERT OR IGNORE INTO share_policy VALUES (?,NULL,'received',0)", (sid,))
        db.execute('UPDATE share_policy SET enabled=0 WHERE share=?', (sid,))


def refresh_share(config, repo, sid):
    policy = repo.one('SELECT * FROM share_policy WHERE share=?', (sid,))
    if not policy or not policy['source']:
        raise ValueError('Select the original file/folder again to refresh this older share')
    return register_share(config, repo, policy['source'], local_selection=policy['approval'] == 'selected', share_id=sid)
