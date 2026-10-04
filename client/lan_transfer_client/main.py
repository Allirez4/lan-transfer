import argparse
import getpass
from pathlib import Path
import secrets
import ssl
import sys
from urllib.parse import urlparse

import httpx

from lan_transfer.storage import ProcessLock
from .state import State, credentials, home_directory, save_credentials
from .transfer import Remote, download, safe_id, upload


def server_url(value):
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Use a server origin such as http://192.168.1.10:8000")
    return value.rstrip('/')


def main():
    parser = argparse.ArgumentParser(description="Resumable, verified LAN file transfers")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pair")
    p.add_argument("--server", required=True)
    p.add_argument("--ca", help="Trusted local CA public root certificate (PEM)")
    p.add_argument("--name", default="Desktop companion")
    p = sub.add_parser("upload")
    p.add_argument("path")
    p.add_argument("--server")
    p.add_argument('--on-conflict', choices=['ask', 'rename', 'skip'], default='ask')
    p.add_argument('--workers', type=int, choices=[1, 2, 4], default=2)
    p = sub.add_parser("download")
    p.add_argument("share_id")
    p.add_argument("--to", required=True)
    p.add_argument("--server")
    p.add_argument('--on-conflict', choices=['ask', 'rename', 'skip'], default='ask')
    p = sub.add_parser("resume")
    p.add_argument("job_id")
    p.add_argument('--on-conflict', choices=['ask', 'rename', 'skip'], help='Override the saved conflict policy')
    p.add_argument('--workers', type=int, choices=[1, 2, 4], default=2)
    sub.add_parser("jobs")
    args = parser.parse_args()
    lock = ProcessLock(home_directory() / "client.lock")
    state = None
    remote = None
    try:
        if args.command == "pair":
            server = server_url(args.server)
            if server.startswith("http:"):
                print("Trusted-LAN HTTP: pairing credentials and file bytes are not encrypted.")
            code = getpass.getpass("Single-use laptop pairing code: ")
            ca = str(Path(args.ca).absolute()) if args.ca else None
            with httpx.Client(verify=ssl.create_default_context(cafile=ca), timeout=30, trust_env=False) as client:
                result = client.post(server + "/api/v1/pair", json={"code": code, "name": args.name, "client": "cli"})
                result.raise_for_status()
                save_credentials(server, result.json()["token"], ca)
            print("Paired. Credentials saved in your user profile.")
            return
        state = State()
        if args.command == "jobs":
            for row in state.db.execute("SELECT id,direction,state FROM jobs ORDER BY rowid DESC"):
                print(f"{row['id']}  {row['direction']}  {row['state']}")
            return
        if args.command == "resume":
            jid = safe_id(args.job_id)
            job = state.one("SELECT * FROM jobs WHERE id=?", (jid,))
            if not job:
                raise ValueError("Unknown local job ID. Run lan-transfer jobs.")
            if args.on_conflict:
                state.execute('UPDATE jobs SET conflict_policy=? WHERE id=?', (args.on_conflict, jid))
                job = state.one('SELECT * FROM jobs WHERE id=?', (jid,))
            server, auth = credentials(job["server"])
        else:
            server, auth = credentials(server_url(args.server) if args.server else None)
            jid = secrets.token_hex(16)
            source = str(Path(args.path).absolute()) if args.command == "upload" else None
            destination = str(Path(args.to).absolute()) if args.command == "download" else None
            share = safe_id(args.share_id) if args.command == "download" else None
            state.execute("INSERT INTO jobs(id,direction,server,source,destination,remote) VALUES (?,?,?,?,?,?)", (jid, args.command, server, source, destination, share))
            state.execute('UPDATE jobs SET conflict_policy=? WHERE id=?', (args.on_conflict, jid))
            job = state.one("SELECT * FROM jobs WHERE id=?", (jid,))
        print(f"Job: {jid}\nResume with: lan-transfer resume {jid}", flush=True)
        state.execute("UPDATE jobs SET state='transferring' WHERE id=?", (jid,))
        remote = Remote(server, auth["token"], auth.get("ca"))
        if job['direction'] == 'upload':
            upload(state, job, remote, workers=args.workers)
        else:
            download(state, job, remote)
        print(f"Completed and verified: {jid}")
    except (ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            print(f"Transfer stopped: HTTP {exc.response.status_code}: {exc.response.text[:500]}", file=sys.stderr)
        else:
            print(f"Transfer stopped: {exc}", file=sys.stderr)
        if state and 'jid' in locals():
            state.execute("UPDATE jobs SET state='paused' WHERE id=?", (jid,))
            print(f"After resolving the issue: lan-transfer resume {jid}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        if state and 'jid' in locals():
            state.execute("UPDATE jobs SET state='paused' WHERE id=?", (jid,))
        print("\nStopped. Durable checkpoints are preserved; use lan-transfer resume <job-id>.", file=sys.stderr)
        raise SystemExit(130) from None
    finally:
        if remote:
            remote.close()
        if state:
            state.close()
        lock.close()


if __name__ == "__main__":
    main()
