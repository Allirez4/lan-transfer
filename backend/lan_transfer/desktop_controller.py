"""Desktop operations separated from Tk, so safety and workflows are testable."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time

import httpx

from .admin import open_repository, submit
from .auth import issue_code
from .config import Config
from .shares import allowed_share, refresh_share, register_share, unshare
from .storage import ProcessLock, fsync_dir


def python_executable():
    # Use python.exe for captured child output even when launched through pythonw.exe.
    candidate = Path(sys.executable).with_name('python.exe')
    return str(candidate) if os.name == 'nt' and candidate.exists() else sys.executable


def launch(module, arguments, cwd=None):
    return subprocess.Popen([python_executable(), '-u', '-m', module, *arguments], cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, encoding='utf-8', errors='replace',
                            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def stop_child(process):
    """Stop only the child explicitly started by this window; checkpoints survive."""
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        process.terminate()
    process.wait(timeout=10)


class DesktopController:
    def __init__(self, config_path='config.json'):
        self.config_path = Path(config_path).absolute()
        self.config = Config.load(self.config_path)

    def repository(self):
        return open_repository(self.config)

    def snapshot(self):
        repo = self.repository()
        try:
            shares = repo.all('SELECT s.*,p.source,p.approval,p.enabled,COUNT(f.id) count,COALESCE(SUM(f.size),0) size FROM shares s LEFT JOIN share_policy p ON p.share=s.id LEFT JOIN files f ON f.share=s.id GROUP BY s.id ORDER BY s.created DESC')
            for row in shares:
                if row['enabled'] == 0:
                    row['status'] = 'Not shared'
                elif not allowed_share(self.config, repo, row):
                    row['status'] = 'Outside allowed roots'
                elif row['source'] and not Path(row['source']).exists():
                    row['status'] = 'Source missing'
                else:
                    row['status'] = 'Shared'
            sessions = repo.all('SELECT id,name,created,revoked FROM sessions ORDER BY created DESC')
            for row in sessions:
                row['status'] = 'Revoked' if row['revoked'] else ('Expired' if row['created'] <= time.time() - self.config.session_days * 86400 else 'Paired')
            jobs = repo.all('SELECT j.*,s.name device,COALESCE(SUM(u.offset),0) committed,COALESCE(SUM(u.size),0) total FROM jobs j LEFT JOIN sessions s ON s.id=j.owner LEFT JOIN uploads u ON u.job=j.id GROUP BY j.id ORDER BY j.created DESC LIMIT 1000')
            commands = repo.all('SELECT * FROM admin_commands ORDER BY created DESC LIMIT 20')
            return {'shares': shares, 'sessions': sessions, 'jobs': jobs, 'commands': commands,
                    'free': shutil.disk_usage(self.config.storage).free}
        finally:
            repo.close()

    def health(self):
        host = '127.0.0.1' if self.config.bind in ('0.0.0.0', '::') else self.config.bind
        try:
            # Windows can take ~2 seconds to report a refused loopback connection.
            with httpx.Client(trust_env=False, timeout=httpx.Timeout(1, connect=3)) as client:
                result = client.get(f'http://{host}:{self.config.port}/api/v1/health', headers={'Host': self.config.hosts[0]})
            if result.json().get('service') != 'LAN Transfer':
                return 'Port used by another service'
            return 'Online' if result.status_code == 200 else 'Needs attention'
        except httpx.TimeoutException:
            return 'Not responding'
        except httpx.ConnectError:
            return 'Stopped'
        except (httpx.HTTPError, ValueError, IndexError):
            return 'Needs attention'

    def pairing_code(self):
        repo = self.repository()
        try:
            return issue_code(repo)
        finally:
            repo.close()

    def add_sources(self, paths):
        repo = self.repository()
        results = []
        try:
            for path in paths:
                try:
                    sid = register_share(self.config, repo, path, local_selection=True)
                    results.append((str(path), sid, None))
                except (ValueError, OSError, sqlite3.Error) as exc:
                    results.append((str(path), None, str(exc)))
            return results
        finally:
            repo.close()

    def share_action(self, sid, action):
        repo = self.repository()
        try:
            if action == 'remove':
                unshare(repo, sid)
            elif action == 'refresh':
                refresh_share(self.config, repo, sid)
            else:
                raise ValueError('Unknown share action')
        finally:
            repo.close()

    def command(self, action, target=''):
        return submit(self.config, action, target)

    def save_config(self, values):
        config = Config(**{**asdict(self.config), **values})
        data = asdict(config)
        data['storage'], data['frontend'] = str(config.storage), str(config.frontend)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as output:
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.config_path)
        fsync_dir(self.config_path.parent)
        self.config = config

    def start_server(self):
        if self.health() != 'Stopped':
            raise ValueError('A server already responds at this address')
        if not (self.config.frontend / 'index.html').exists():
            raise ValueError('Build the browser interface first with scripts/setup.ps1')
        return launch('lan_transfer.cli', ['--config', str(self.config_path), 'serve'], str(self.config_path.parent))

    def pair_remote(self, server, code, ca=None):
        from lan_transfer_client.main import server_url
        from lan_transfer_client.state import home_directory, save_credentials
        server = server_url(server)
        lock = ProcessLock(home_directory() / 'client.lock')
        try:
            with httpx.Client(verify=ssl.create_default_context(cafile=ca or None), timeout=30, trust_env=False) as client:
                result = client.post(server + '/api/v1/pair', json={'code': code, 'name': 'Laptop desktop app', 'client': 'cli'})
                result.raise_for_status()
                save_credentials(server, result.json()['token'], str(Path(ca).absolute()) if ca else None)
        finally:
            lock.close()
        return server

    def remote_shares(self, server):
        from lan_transfer_client.state import credentials
        from lan_transfer_client.transfer import Remote
        server, auth = credentials(server or None)
        remote = Remote(server, auth['token'], auth.get('ca'))
        try:
            rows = []
            while True:
                last = rows[-1] if rows else {'created': 0, 'id': ''}
                page = remote.get(f"shares?limit=500&after={last['created']}&after_id={last['id']}")
                rows.extend(page)
                if len(page) < 500:
                    return rows
        finally:
            remote.close()

    def client_jobs(self):
        from lan_transfer_client.state import home_directory
        path = home_directory() / 'jobs.sqlite3'
        if not path.exists():
            return []
        db = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY rowid DESC LIMIT 1000')]
        finally:
            db.close()

    def pause_companion(self, process):
        from lan_transfer_client.state import State, home_directory
        stop_child(process)
        lock = ProcessLock(home_directory() / 'client.lock')
        try:
            state = State()
            try:
                state.execute("UPDATE jobs SET state='paused' WHERE state='transferring'")
            finally:
                state.close()
        finally:
            lock.close()
