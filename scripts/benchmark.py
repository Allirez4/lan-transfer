"""Opt-in materialized-byte benchmark. Never creates a large dataset implicitly."""
import argparse
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import time

from lan_transfer_client.state import State, credentials
from lan_transfer_client.transfer import Remote, upload, download


class TimedRemote(Remote):
    verification_observation_latency_sum = 0.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.verifying = {}

    def post(self, path, body=None):
        result = super().post(path, body)
        if path.endswith('/complete'):
            self.verifying[path.rsplit('/', 1)[0]] = time.perf_counter()
        return result

    def get(self, path):
        result = super().get(path)
        if path in self.verifying and result.get('state') == 'completed':
            self.verification_observation_latency_sum += time.perf_counter() - self.verifying.pop(path)
        return result


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', help='Previously paired server; defaults to last paired server')
    parser.add_argument('--workdir', required=True, type=Path, help='Local volume for source and downloaded materialized bytes')
    parser.add_argument('--bytes', type=int, default=64 * 1024 * 1024)
    parser.add_argument('--files', type=int, default=1)
    parser.add_argument('--confirm-large', action='store_true', help='Required above 1 GiB')
    args = parser.parse_args()
    if args.bytes <= 0 or args.files <= 0 or args.files > args.bytes:
        parser.error('Use positive bytes/files with at least one byte per file')
    if args.bytes > 1024**3 and not args.confirm_large:
        parser.error('Large materialized tests require --confirm-large. Plan for source + server copy + download storage.')
    args.workdir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.workdir).free < args.bytes * 2 + 1024**3:
        parser.error('Insufficient client disk space for materialized source + downloaded copy + 1 GiB margin')
    server, auth = credentials(args.server)
    remote = TimedRemote(server, auth['token'], auth.get('ca'))
    cap = remote.get('capabilities')
    if cap['disk']['available'] < args.bytes:
        parser.error('Server cannot admit the requested materialized dataset')
    run_id = secrets.token_hex(16)
    output = args.workdir.absolute() / ('benchmark-' + run_id)
    output.mkdir()
    source = output / 'source'
    source.mkdir()
    destination = output / 'downloaded'
    (source / 'empty' / 'nested').mkdir(parents=True)
    manifest = {}
    generated = time.perf_counter()
    for index in range(args.files):
        size = args.bytes // args.files + (1 if index < args.bytes % args.files else 0)
        path = source / ('folder-' + str(index % 16)) / f'file-{index:06d}.bin'
        path.parent.mkdir(exist_ok=True)
        digest = hashlib.sha256()
        with path.open('xb') as f:
            remaining = size
            while remaining:
                block = os.urandom(min(4 * 1024 * 1024, remaining))
                f.write(block)
                digest.update(block)
                remaining -= len(block)
            f.flush()
            os.fsync(f.fileno())
        manifest[path.relative_to(source).as_posix()] = {'bytes': size, 'sha256': digest.hexdigest()}
    generation_seconds = time.perf_counter() - generated
    (output / 'source-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    state = State(output / 'benchmark-state.sqlite3')
    upload_id, download_id = secrets.token_hex(16), secrets.token_hex(16)
    state.execute('INSERT INTO jobs(id,direction,server,source) VALUES (?,?,?,?)', (upload_id, 'upload', server, str(source)))
    with (output / 'transfer.log').open('w', encoding='utf-8') as log, redirect_stdout(log):
        cpu_start, start = time.process_time(), time.perf_counter()
        upload(state, state.one('SELECT * FROM jobs WHERE id=?', (upload_id,)), remote)
        upload_seconds = time.perf_counter() - start
        uploaded = state.one('SELECT * FROM jobs WHERE id=?', (upload_id,))
        state.execute('INSERT INTO jobs(id,direction,server,destination,remote) VALUES (?,?,?,?,?)', (download_id, 'download', server, str(destination), uploaded['remote']))
        start = time.perf_counter()
        download(state, state.one('SELECT * FROM jobs WHERE id=?', (download_id,)), remote)
        download_seconds = time.perf_counter() - start
        client_cpu_seconds = time.process_time() - cpu_start
    result = {
        'run_id': run_id, 'platform': platform.platform(), 'python': platform.python_version(),
        'dataset': 'os.urandom materialized bytes; no sparse/zero-file throughput claim',
        'bytes': args.bytes, 'files': args.files, 'chunk_bytes': cap['chunk_size'],
        'server_active_file_limit': cap['active_files'], 'client_active_files': 2,
        'generation_seconds': generation_seconds, 'upload_including_verification_seconds': upload_seconds,
        'sum_verification_observation_latency_seconds': remote.verification_observation_latency_sum,
        'download_including_disk_verification_seconds': download_seconds,
        'upload_end_to_end_MB_per_second': args.bytes / upload_seconds / 1e6,
        'download_end_to_end_MB_per_second': args.bytes / download_seconds / 1e6,
        'client_cpu_seconds': client_cpu_seconds,
        'network_only_Mbps': None, 'peak_server_RSS_MiB': None, 'peak_client_RSS_MiB': None,
        'limitations': 'RSS, disk-only and network-only baselines must be recorded separately; this CLI run allows two active upload files. Localhost is not Wi-Fi.',
        'server_share_id': uploaded['remote'],
    }
    (output / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    state.close()
    remote.close()
    print('Local benchmark resume state is stored in the artifact directory, separately from your normal companion jobs.')
    print(json.dumps(result, indent=2))
    print(f'Artifacts retained at {output}; server copy retained as share {uploaded["remote"]}. No source files deleted.')


if __name__ == '__main__':
    run()
