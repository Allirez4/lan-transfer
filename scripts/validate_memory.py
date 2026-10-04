"""Materialized two-client upload test with process memory sampled separately from disk cache."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx
import psutil
from lan_transfer.auth import issue_code
from lan_transfer.repository import Repository
from lan_transfer_client.state import State
from lan_transfer_client.transfer import Remote, upload
from process_utils import stop_tree, tree_usage


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    result = {'files': 2, 'bytes_per_file': 256 * 1024**2, 'client_active_files': 2, 'chunk_bytes': 16 * 1024**2,
              'dataset': 'Two materialized random files, independent simultaneous CLI uploads',
              'peak_server_tree_RSS_MiB': 0, 'peak_client_RSS_MiB': 0, 'network': 'Loopback; other local tests may run concurrently'}
    with tempfile.TemporaryDirectory(prefix='generated-memory-', dir=output) as directory:
        root = Path(directory).resolve()
        assert root.is_relative_to(output.resolve())
        storage = root / 'server'; storage.mkdir()
        manifest = {}
        for i in range(2):
            digest = hashlib.sha256()
            with (root / f'{i}.bin').open('xb') as target:
                for _ in range(64):
                    block = os.urandom(4 * 1024**2)
                    digest.update(block); target.write(block)
                target.flush(); os.fsync(target.fileno())
            manifest[i] = digest.hexdigest()
        del block
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        config = root / 'config.json'
        config.write_text(json.dumps({'storage': str(storage), 'hosts': ['127.0.0.1'], 'origins': [url], 'safety_bytes': 0}), encoding='utf-8')
        repo = Repository(storage / 'state.sqlite3'); code = issue_code(repo); repo.close()
        env = {**os.environ, 'LAN_TRANSFER_CONFIG': str(config)}
        with (output / 'server.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'lan_transfer.api:create_app', '--factory', '--host', '127.0.0.1', '--port', str(port), '--no-access-log'],
                                       stdout=log, stderr=log, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            stop = threading.Event()
            def sample():
                while not stop.wait(.1):
                    result['peak_server_tree_RSS_MiB'] = max(result['peak_server_tree_RSS_MiB'], tree_usage(process.pid)[0] / 1024**2)
                    result['peak_client_RSS_MiB'] = max(result['peak_client_RSS_MiB'], psutil.Process().memory_info().rss / 1024**2)
            sampler = threading.Thread(target=sample, daemon=True); sampler.start()
            try:
                with httpx.Client(timeout=1) as client:
                    for _ in range(100):
                        try:
                            if client.get(url + '/api/v1/health').is_success: break
                        except httpx.TransportError: pass
                        time.sleep(.1)
                    response = client.post(url + '/api/v1/pair', json={'code': code, 'client': 'cli'})
                    response.raise_for_status(); token = response.json()['token']
                start_gate = threading.Barrier(2)
                def worker(index):
                    state = State(root / f'client-{index}.sqlite3')
                    remote = Remote(url, token)
                    try:
                        jid = secrets.token_hex(16)
                        state.execute('INSERT INTO jobs(id,direction,server,source) VALUES (?,?,?,?)', (jid, 'upload', url, str(root / f'{index}.bin')))
                        start_gate.wait(timeout=15)
                        upload(state, state.one('SELECT * FROM jobs WHERE id=?', (jid,)), remote)
                        share = state.one('SELECT remote FROM jobs WHERE id=?', (jid,))['remote']
                        row = remote.get(f'jobs/{share}/entries')[0]
                        assert row['state'] == 'completed' and row['sha256'] == manifest[index] and row['size'] == result['bytes_per_file']
                    finally:
                        state.close(); remote.close()
                start = time.perf_counter()
                with (output / 'transfers.log').open('w', encoding='utf-8') as log, redirect_stdout(log), ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(worker, range(2)))
                result['wall_seconds_including_verification'] = time.perf_counter() - start
                result['server_cpu_seconds'] = tree_usage(process.pid)[1]
                result['status'] = 'passed'
                result['server_under_256_MiB'] = result['peak_server_tree_RSS_MiB'] < 256
            finally:
                stop.set(); sampler.join(); stop_tree(process)
                (output / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    run(Path(__file__).resolve().parents[1] / 'artifacts' / 'memory-validation')
