"""Opt-in loopback validation with real random bytes and isolated, disposable storage."""
import argparse
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
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
from lan_transfer.storage import digest_file
from lan_transfer_client.state import State
from lan_transfer_client.transfer import upload, download
from benchmark import TimedRemote
from process_utils import stop_tree, tree_usage


def validate(size, output, files=1):
    if shutil.disk_usage(output).free < size * 3 + 2 * 1024**3:
        raise ValueError('Need source + server + downloaded bytes (3× dataset) plus 2 GiB free on this volume')
    result = {'bytes': size, 'files': files, 'platform': platform.platform(), 'python': platform.python_version(),
              'network': '127.0.0.1 loopback; not Wi-Fi', 'dataset': 'Materialized os.urandom bytes; no sparse files',
              'chunk_bytes': 16 * 1024**2, 'server_active_file_limit': 2, 'client_active_files': 2}
    with tempfile.TemporaryDirectory(prefix='generated-validation-', dir=output) as directory:
        root = Path(directory).resolve()
        if not root.is_relative_to(output.resolve()):
            raise ValueError('Temporary fixture directory escaped the requested output root')
        storage, source, destination = root / 'server', root / 'source', root / 'download'
        storage.mkdir(); source.mkdir()
        (source / 'empty' / 'nested').mkdir(parents=True)
        manifest = {}
        start = time.perf_counter()
        for index in range(files):
            if files == 1:
                count = size
            elif index == 0:
                count = size // 2
            else:
                remaining = size - size // 2
                count = remaining // (files - 1) + (index - 1 < remaining % (files - 1))
            relative = f'folder-{index % 16}/سلام-{index:05}.bin'
            path = source / relative
            path.parent.mkdir(exist_ok=True)
            digest = hashlib.sha256()
            with path.open('xb') as target:
                remaining = count
                while remaining:
                    block = os.urandom(min(4 * 1024**2, remaining))
                    target.write(block); digest.update(block); remaining -= len(block)
                target.flush(); os.fsync(target.fileno())
            manifest[relative] = {'size': count, 'sha256': digest.hexdigest()}
        result['generation_seconds'] = time.perf_counter() - start
        result['source_manifest'] = manifest
        print(f'Generated {size:,} real bytes; starting upload and verified download.', flush=True)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        server_url = f'http://127.0.0.1:{port}'
        config = root / 'config.json'
        config.write_text(json.dumps({'storage': str(storage), 'hosts': ['127.0.0.1'], 'origins': [server_url],
                                     'bind': '127.0.0.1', 'port': port, 'safety_bytes': 1024**3}), encoding='utf-8')
        repo = Repository(storage / 'state.sqlite3')
        code = issue_code(repo); repo.close()
        env = {**os.environ, 'LAN_TRANSFER_CONFIG': str(config)}
        with (output / f'{size}-{files}-server.log').open('w', encoding='utf-8') as server_log:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'lan_transfer.api:create_app', '--factory',
                                        '--host', '127.0.0.1', '--port', str(port), '--no-access-log'], env=env,
                                       stdout=server_log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            stop = threading.Event()
            peaks = {'server': 0, 'client': 0}
            def sample():
                while not stop.wait(.1):
                    for key, pid in [('server', process.pid), ('client', os.getpid())]:
                        try:
                            rss = tree_usage(pid)[0] if key == 'server' else psutil.Process(pid).memory_info().rss
                            peaks[key] = max(peaks[key], rss)
                        except psutil.Error:
                            pass
            sampler = threading.Thread(target=sample, daemon=True)
            sampler.start()
            remote = state = None
            try:
                with httpx.Client(timeout=1) as client:
                    for _ in range(100):
                        if process.poll() is not None:
                            raise RuntimeError('Test server failed; inspect its retained log')
                        try:
                            if client.get(server_url + '/api/v1/health').is_success:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(.1)
                    paired = client.post(server_url + '/api/v1/pair', json={'code': code, 'client': 'cli'})
                    paired.raise_for_status()
                remote = TimedRemote(server_url, paired.json()['token'])
                state = State(root / 'client.sqlite3')
                uid, did = secrets.token_hex(16), secrets.token_hex(16)
                state.execute('INSERT INTO jobs(id,direction,server,source) VALUES (?,?,?,?)', (uid, 'upload', server_url, str(source)))
                with (output / f'{size}-{files}-transfer.log').open('w', encoding='utf-8') as transfer_log, redirect_stdout(transfer_log):
                    cpu, start = time.process_time(), time.perf_counter()
                    upload(state, state.one('SELECT * FROM jobs WHERE id=?', (uid,)), remote)
                    result['upload_including_verification_seconds'] = time.perf_counter() - start
                    share = state.one('SELECT remote FROM jobs WHERE id=?', (uid,))['remote']
                    state.execute('INSERT INTO jobs(id,direction,server,destination,remote) VALUES (?,?,?,?,?)', (did, 'download', server_url, str(destination), share))
                    start = time.perf_counter()
                    download(state, state.one('SELECT * FROM jobs WHERE id=?', (did,)), remote)
                    result['download_including_disk_verification_seconds'] = time.perf_counter() - start
                    result['client_cpu_seconds'] = time.process_time() - cpu
                # Independently re-read the published destinations after the client reports success.
                start = time.perf_counter()
                for relative, expected in manifest.items():
                    path = destination / source.name / relative
                    assert path.stat().st_size == expected['size']
                    assert digest_file(path) == expected['sha256']
                assert (destination / source.name / 'empty' / 'nested').is_dir()
                result['independent_destination_recheck_seconds'] = time.perf_counter() - start
                result['sum_verification_observation_latency_seconds'] = remote.verification_observation_latency_sum
                result['server_cpu_seconds'] = tree_usage(process.pid)[1]
                result['status'] = 'passed'
                for direction in ('upload', 'download'):
                    seconds = result['upload_including_verification_seconds' if direction == 'upload' else 'download_including_disk_verification_seconds']
                    result[direction + '_end_to_end_MB_per_second'] = size / seconds / 1e6
            finally:
                if state: state.close()
                if remote: remote.close()
                stop.set(); sampler.join()
                stop_tree(process)
                result['peak_server_RSS_MiB'] = peaks['server'] / 1024**2
                result['peak_client_RSS_MiB'] = peaks['client'] / 1024**2
                result['measurement_notes'] = 'Server RSS/CPU includes the Windows venv launcher and child process. Other local development work may run concurrently; no isolated disk/network baseline.'
                result['fixture_cleanup'] = 'Only generated source, server and download fixtures removed after run; logs and checksums retained'
                (output / f'{size}-{files}-results.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'source_manifest'}, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bytes', type=int, nargs='+', required=True)
    parser.add_argument('--files', type=int, default=1)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--confirm-large', action='store_true')
    args = parser.parse_args()
    if any(n <= 0 or args.files <= 0 or args.files > n for n in args.bytes):
        parser.error('Use positive byte/file counts with at least one byte per file')
    if max(args.bytes) > 1024**3 and not args.confirm_large:
        parser.error('Above 1 GiB requires --confirm-large; allow 3× dataset size + 2 GiB disk space')
    args.output = args.output.absolute()
    args.output.mkdir(parents=True, exist_ok=True)
    for size in args.bytes:
        validate(size, args.output, args.files)
