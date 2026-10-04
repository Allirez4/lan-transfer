import json
import os
from pathlib import Path
import sqlite3
from contextlib import contextmanager

from lan_transfer.storage import fsync_dir


def home_directory():
    path = Path(os.environ.get("LAN_TRANSFER_CLIENT_HOME", Path.home() / ".lan-transfer"))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def save_credentials(server, token, ca=None):
    path = home_directory() / "credentials.json"
    existing = json.loads(path.read_text("utf-8")) if path.exists() else {}
    existing[server] = {"token": token, "ca": ca}
    existing["default_server"] = server
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(existing, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    fsync_dir(path.parent)


def credentials(server=None):
    path = home_directory() / "credentials.json"
    if not path.exists():
        raise ValueError("Pair first: lan-transfer pair --server http://<laptop-ip>:8000")
    data = json.loads(path.read_text("utf-8"))
    server = server or data["default_server"]
    if server not in data:
        raise ValueError("This server has not been paired")
    return server, data[server]


class State:
    def __init__(self, path=None):
        self.path = path or home_directory() / "jobs.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, direction TEXT, server TEXT, source TEXT, destination TEXT, remote TEXT, sealed INTEGER DEFAULT 0, state TEXT DEFAULT 'queued');
        CREATE TABLE IF NOT EXISTS entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, id TEXT, path TEXT, kind TEXT, size INTEGER, version TEXT, sha256 TEXT, modified INTEGER DEFAULT 0, offset INTEGER DEFAULT 0, prefix TEXT, state TEXT DEFAULT 'queued', UNIQUE(job,path));
        CREATE INDEX IF NOT EXISTS entries_job ON entries(job,seq);
        """)
        if 'conflict_policy' not in {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN conflict_policy TEXT DEFAULT 'ask'")
        if 'destination_path' not in {r[1] for r in self.db.execute('PRAGMA table_info(entries)')}:
            self.db.execute("ALTER TABLE entries ADD COLUMN destination_path TEXT")
        self.db.commit()
        self.batch_depth = 0

    def execute(self, sql, args=()):
        if self.batch_depth:
            self.db.execute(sql, args)
            return
        with self.db:
            self.db.execute(sql, args)

    @contextmanager
    def batch(self):
        """Commit bounded metadata pages together; byte checkpoints stay independent."""
        with self.db:
            self.batch_depth += 1
            try:
                yield
            finally:
                self.batch_depth -= 1

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def pages(self, jid):
        after = 0
        while True:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM entries WHERE job=? AND seq>? ORDER BY seq LIMIT 500", (jid, after))]
            if not rows:
                return
            yield rows
            after = rows[-1]["seq"]

    def close(self):
        self.db.close()
