import hashlib
import secrets
import time
from fastapi import HTTPException


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


def issue_code(repo):
    code = secrets.token_hex(4).upper()
    repo.execute("INSERT INTO pairing VALUES (?,?)", (hashed(code), time.time() + 300))
    return code


def pair(repo, code, name, address):
    now = time.time()
    # Commit failed attempts too, so rejected transactions cannot reset rate limiting.
    with repo.transaction() as db:
        row = db.execute("SELECT * FROM attempts WHERE address=?", (address,)).fetchone()
        if row and now - row["start"] < 300 and row["count"] >= 10:
            raise HTTPException(429, "Too many pairing attempts. Wait five minutes.", headers={"Retry-After": "300"})
        if not row or now - row["start"] >= 300:
            db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,1)", (address, now))
        else:
            db.execute("UPDATE attempts SET count=count+1 WHERE address=?", (address,))
    with repo.transaction() as db:
        result = db.execute("DELETE FROM pairing WHERE hash=? AND expires>?", (hashed(code.strip().upper()), now))
        if result.rowcount != 1:
            raise HTTPException(401, "Pairing code is invalid, expired, or already used")
        token, sid = secrets.token_urlsafe(32), secrets.token_hex(16)
        db.execute("INSERT INTO sessions(id,hash,name,created) VALUES (?,?,?,?)", (sid, hashed(token), name, now))
    return token, sid


def authenticate(repo, token, session_days=30):
    session = repo.one("SELECT id,name FROM sessions WHERE hash=? AND revoked=0 AND created>?",
                       (hashed(token), time.time() - session_days * 86400))
    if not session:
        raise HTTPException(401, "Pair this device with the laptop to continue")
    return session
