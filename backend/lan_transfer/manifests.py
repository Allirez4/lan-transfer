"""Durable source-to-destination mappings and explicit conflict resolution."""
from pathlib import PurePosixPath

from fastapi import HTTPException

from .storage import relative_path


def mapped_path(db, job, source):
    parts = source.split('/')
    for count in range(len(parts), 0, -1):
        prefix = '/'.join(parts[:count])
        mapping = db.execute('SELECT target FROM path_mappings WHERE job=? AND source=?', (job, prefix)).fetchone()
        if mapping:
            return relative_path('/'.join([mapping['target'], *parts[count:]]))
    return relative_path(source)


def conflict(db, job, path, kind, exclude=''):
    parts = path.split('/')
    for count in range(1, len(parts) + 1):
        prefix = '/'.join(parts[:count])
        norm = prefix.casefold()
        existing = db.execute('SELECT path,kind FROM uploads WHERE job=? AND norm=? AND id!=?', (job, norm, exclude)).fetchone()
        if existing:
            if count == len(parts) or existing['kind'] == 'file' or existing['path'] != prefix:
                return {'component': count - 1, 'existing_path': existing['path'], 'reason': f"Name conflicts with {existing['path']}"}
        child = db.execute('SELECT path FROM uploads WHERE job=? AND norm>=? AND norm<? AND id!=? LIMIT 1', (job, norm + '/', norm + '0', exclude)).fetchone()
        if child and (child['path'].split('/')[:count] != parts[:count] or (count == len(parts) and kind == 'file')):
            return {'component': count - 1, 'existing_path': child['path'], 'reason': f"File/directory or case collision with {child['path']}"}
    return None


def suggestion(db, row):
    path = row['path']
    issue = conflict(db, row['job'], path, row['kind'], row['id'])
    if not issue:
        return path
    parts = path.split('/')
    index = issue['component']
    original = parts[index]
    extension = PurePosixPath(original).suffix if index == len(parts) - 1 and row['kind'] == 'file' else ''
    stem = original[:-len(extension)] if extension else original
    for number in range(2, 10002):
        parts[index] = f'{stem} ({number}){extension}'
        candidate = '/'.join(parts)
        if not conflict(db, row['job'], candidate, row['kind'], row['id']):
            return candidate
    raise HTTPException(409, 'Choose a unique destination path')


def describe(repo, rows):
    with repo.lock:
        for row in rows:
            if row['state'] == 'conflict':
                row['suggested_path'] = suggestion(repo.db, row)
    return rows


def resolve(repo, uid, action, path=None):
    with repo.transaction() as db:
        row = dict(db.execute('SELECT * FROM uploads WHERE id=?', (uid,)).fetchone())
        job = db.execute('SELECT state FROM jobs WHERE id=?', (row['job'],)).fetchone()
        if (action == 'skip' and row['state'] == 'skipped') or (action == 'rename' and row['resolution'] == 'rename' and (path is None or relative_path(path) == row['path'])):
            return
        if job['state'] != 'registering' or row['offset'] or row['state'] not in ('queued', 'conflict'):
            raise HTTPException(409, 'Resolve names before sealing or transferring this batch')
        if action == 'skip':
            db.execute("UPDATE uploads SET state='skipped',norm=NULL,resolution='skip',error='Skipped by user' WHERE id=?", (uid,))
            if row['kind'] == 'directory':
                prefix = row['source_path'] + '/'
                db.execute("UPDATE uploads SET state='skipped',norm=NULL,resolution='skip',error='Parent directory skipped by user' WHERE job=? AND source_path>=? AND source_path<?", (row['job'], prefix, row['source_path'] + '0'))
            return
        target = relative_path(path or suggestion(db, row))
        issue = conflict(db, row['job'], target, row['kind'], uid)
        if issue:
            raise HTTPException(409, issue['reason'])
        db.execute("UPDATE uploads SET path=?,norm=?,state='queued',resolution='rename',error=NULL WHERE id=?", (target, target.casefold(), uid))
        if row['kind'] == 'directory':
            db.execute('INSERT OR REPLACE INTO path_mappings VALUES (?,?,?)', (row['job'], row['source_path'], target))
            prefix = row['source_path'] + '/'
            # Free descendant claims together, then revalidate the mapped tree in stable order.
            db.execute("UPDATE uploads SET norm=NULL WHERE job=? AND source_path>=? AND source_path<? AND state!='skipped'", (row['job'], prefix, row['source_path'] + '0'))
            after = 0
            while descendants := db.execute("SELECT * FROM uploads WHERE job=? AND source_path>=? AND source_path<? AND seq>? AND state!='skipped' ORDER BY seq LIMIT 500", (row['job'], prefix, row['source_path'] + '0', after)).fetchall():
                for descendant in descendants:
                    after = descendant['seq']
                    output = mapped_path(db, row['job'], descendant['source_path'])
                    issue = conflict(db, row['job'], output, descendant['kind'], descendant['id'])
                    db.execute('UPDATE uploads SET path=?,norm=?,state=?,error=? WHERE id=?', (output, None if issue else output.casefold(), 'conflict' if issue else 'queued', issue['reason'] if issue else None, descendant['id']))
