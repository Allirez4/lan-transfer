import argparse
import os
from pathlib import Path
import secrets
import time

import qrcode
import uvicorn

from .auth import issue_code
from .config import Config
from .repository import Repository
from .storage import confined, is_link, relative_path, version


from .shares import register_share


def main():
    parser = argparse.ArgumentParser(description="Local LAN Transfer server administration")
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser('gui')
    sub.add_parser('shares')
    sub.add_parser('jobs')
    sub.add_parser('stop')
    for command in ('unshare', 'refresh-share'):
        sub.add_parser(command).add_argument('share_id')
    for command in ('pause', 'resume', 'cancel', 'discard'):
        sub.add_parser(command).add_argument('job_id')
    sub.add_parser("pair-code")
    p = sub.add_parser("share")
    p.add_argument("path")
    sub.add_parser("sessions")
    p = sub.add_parser("revoke")
    p.add_argument("session_id")
    args = parser.parse_args()
    config = Config.load(args.config)
    if args.command == 'gui':
        from .desktop import main as desktop
        desktop(args.config)
        return
    from .admin import open_repository, submit
    if args.command in ('revoke', 'pause', 'resume', 'cancel', 'discard', 'stop'):
        cid = submit(config, args.command, getattr(args, 'session_id', getattr(args, 'job_id', '')))
        print(f'Local command: {cid} (processed immediately when offline, or by the running server)')
        return
    repo = open_repository(config)
    try:
        if args.command in ("serve", "pair-code"):
            print(f"Single-use pairing code (valid 5 minutes): {issue_code(repo)}", flush=True)
        elif args.command == "share":
            print(f"Registered share: {register_share(config, repo, args.path)}")
        elif args.command == "sessions":
            for row in repo.all("SELECT id,name,revoked FROM sessions ORDER BY created"):
                print(f"{row['id']}  {row['name']}  {'revoked' if row['revoked'] else 'active'}")
        elif args.command == 'shares':
            for row in repo.all('SELECT s.*,p.enabled FROM shares s LEFT JOIN share_policy p ON p.share=s.id ORDER BY s.created'):
                print(f"{row['id']}  {row['name']}  {'unshared' if row['enabled'] == 0 else 'published'}")
        elif args.command == 'jobs':
            for row in repo.all('SELECT id,name,state,owner FROM jobs ORDER BY created'):
                print(f"{row['id']}  {row['state']}  {row['name']}  device={row['owner']}")
        elif args.command == 'unshare':
            from .shares import unshare
            unshare(repo, args.share_id)
            print('Removed from sharing. Original files are unchanged.')
        elif args.command == 'refresh-share':
            from .shares import refresh_share
            refresh_share(config, repo, args.share_id)
            print('Share refreshed. Existing downloads must start again with the new file versions.')
    finally:
        repo.close()
    if args.command == "serve":
        from .api import create_app
        print("LAN Transfer: keep the laptop awake. Use the Private network firewall profile.")
        print("If unreachable: check Wi-Fi client isolation, VPN routing, firewall, and the laptop IP.")
        if not config.secure:
            print("Trusted-LAN HTTP: pairing credentials and files are not encrypted. Use HTTPS for sensitive transfers.")
        for url in config.origins:
            print(f"Open: {url}")
        chosen = next((url for url in config.origins if "127.0.0.1" not in url and "localhost" not in url), config.origins[0])
        qr = qrcode.QRCode(border=1)
        qr.add_data(chosen)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        app = create_app(config)
        server = uvicorn.Server(uvicorn.Config(app, host=config.bind, port=config.port, workers=1, loop='asyncio', access_log=False,
                    limit_concurrency=64, timeout_keep_alive=30, timeout_graceful_shutdown=5,
                    proxy_headers=config.secure, forwarded_allow_ips='127.0.0.1'))
        app.state.shutdown = lambda: setattr(server, 'should_exit', True)
        server.run()


if __name__ == "__main__":
    main()
