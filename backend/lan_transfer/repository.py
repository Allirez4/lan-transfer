from contextlib import contextmanager
import sqlite3
import threading


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pairing (hash TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (address TEXT PRIMARY KEY, start REAL, count INTEGER);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, hash TEXT UNIQUE, name TEXT, created REAL, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT, key TEXT, name TEXT, state TEXT DEFAULT 'registering', created REAL, UNIQUE(owner,key));
CREATE TABLE IF NOT EXISTS uploads (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, job TEXT, path TEXT, norm TEXT, kind TEXT, size INTEGER, modified INTEGER, chunk_size INTEGER, offset INTEGER DEFAULT 0, state TEXT DEFAULT 'queued', sha256 TEXT, error TEXT, UNIQUE(job,norm));
CREATE TABLE IF NOT EXISTS receipts (upload TEXT, offset INTEGER, length INTEGER, sha256 TEXT, request_id TEXT, PRIMARY KEY(upload,offset), UNIQUE(upload,request_id));
CREATE TABLE IF NOT EXISTS shares (id TEXT PRIMARY KEY, name TEXT, root TEXT, created REAL);
CREATE TABLE IF NOT EXISTS files (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, share TEXT, path TEXT, kind TEXT, size INTEGER, version TEXT, sha256 TEXT, UNIQUE(share,path));
CREATE INDEX IF NOT EXISTS uploads_job ON uploads(job,seq);
CREATE INDEX IF NOT EXISTS files_share ON files(share,seq);
CREATE TABLE IF NOT EXISTS path_mappings(job TEXT, source TEXT, target TEXT, PRIMARY KEY(job,source));
CREATE TABLE IF NOT EXISTS share_policy(share TEXT PRIMARY KEY, source TEXT, approval TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS admin_commands(id TEXT PRIMARY KEY, action TEXT NOT NULL, target TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', error TEXT, created REAL NOT NULL);
"""


class Repository:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        # Additive migration preserves jobs created by the first version.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(uploads)")}
        for name, definition in {"source_key": "TEXT", "source_path": "TEXT", "resolution": "TEXT DEFAULT 'original'"}.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE uploads ADD COLUMN {name} {definition}")
        self.db.execute("UPDATE uploads SET source_key=path,source_path=path WHERE source_key IS NULL")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uploads_source ON uploads(job,source_key)")
        self.db.execute("CREATE INDEX IF NOT EXISTS uploads_source_path ON uploads(job,source_path)")
        if 'expected_entries' not in {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN expected_entries INTEGER")
        if 'discarded' not in {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN discarded INTEGER DEFAULT 0")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    def all(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.transaction() as db:
            db.execute(sql, args)

    def close(self):
        self.db.close()
