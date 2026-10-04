"""Real TLS sockets, with an isolated CA; never change the system trust store."""
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from lan_transfer.auth import issue_code
from lan_transfer.repository import Repository
from process_utils import stop_tree


def unused_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_trusted_https_upload_range_and_secure_cookie(tmp_path):
    port, backend_port = unused_port(), unused_port()
    origin = f'https://127.0.0.1:{port}'
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'storage': str(tmp_path / 'server'), 'secure': True, 'hosts': ['127.0.0.1'],
                                 'origins': [origin], 'safety_bytes': 0}), encoding='utf-8')
    (tmp_path / 'server').mkdir()
    repo = Repository(tmp_path / 'server' / 'state.sqlite3')
    code = issue_code(repo)
    repo.close()
    env = {**os.environ, 'LAN_TRANSFER_CONFIG': str(config)}
    command = [sys.executable, '-m', 'uvicorn', 'lan_transfer.api:create_app', '--factory', '--host', '127.0.0.1', '--no-access-log']
    caddy = os.environ.get('LAN_TRANSFER_TEST_CADDY')
    processes = []
    with (tmp_path / 'https.log').open('w') as log:
        try:
            if caddy:
                command.extend(['--port', str(backend_port)])
                caddyfile = tmp_path / 'Caddyfile'
                caddyfile.write_text('''{
    admin off
    persist_config off
    auto_https disable_redirects
    skip_install_trust
    storage file_system {
        root "''' + (tmp_path / 'ca').as_posix() + '''"
    }
}
''' + origin + ''' {
    bind 127.0.0.1
    tls internal
    reverse_proxy 127.0.0.1:''' + str(backend_port) + '''
}
''', encoding='utf-8')
                ca_path = tmp_path / 'ca' / 'pki' / 'authorities' / 'local' / 'root.crt'
            else:
                # This certificate is trusted only by the test's explicit SSLContext.
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'LAN Transfer disposable TLS test')])
                now = datetime.datetime.now(datetime.timezone.utc)
                cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
                        .not_valid_after(now + datetime.timedelta(hours=1))
                        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                        .sign(key, hashes.SHA256()))
                ca_path = tmp_path / 'root.crt'
                ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
                key_path = tmp_path / 'server.key'
                key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
                command.extend(['--port', str(port), '--ssl-certfile', str(ca_path), '--ssl-keyfile', str(key_path)])
            processes.append(subprocess.Popen(command, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
            if caddy:
                processes.append(subprocess.Popen([caddy, 'run', '--config', str(caddyfile), '--adapter', 'caddyfile'],
                                                  stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
            for _ in range(200):
                assert all(p.poll() is None for p in processes), (tmp_path / 'https.log').read_text()
                if ca_path.exists():
                    try:
                        with httpx.Client(verify=ssl.create_default_context(cafile=str(ca_path)), timeout=.5) as probe:
                            if probe.get(origin + '/api/v1/health').is_success:
                                break
                    except httpx.TransportError:
                        pass
                time.sleep(.05)
            else:
                pytest.fail('TLS service did not become ready')
            with httpx.Client(timeout=5) as untrusted:
                with pytest.raises(httpx.ConnectError, match='CERTIFICATE_VERIFY_FAILED'):
                    untrusted.get(origin + '/api/v1/health')
            with httpx.Client(base_url=origin + '/api/v1/', verify=ssl.create_default_context(cafile=str(ca_path)), timeout=10, headers={'Origin': origin}) as client:
                result = client.post('pair', json={'code': code, 'client': 'browser'})
                assert result.status_code == 200
                cookie = result.headers['set-cookie'].lower()
                assert 'secure' in cookie and 'httponly' in cookie and 'samesite=strict' in cookie
                assert client.get('capabilities').json()['secure'] is True
                jid = client.post('jobs', json={'key': 'tls-test', 'name': 'TLS'}).json()['id']
                content = os.urandom(1024**2 + 17)
                assert client.post(f'jobs/{jid}/manifest', json={'entries': [{'path': 'سلام.bin', 'size': len(content)}]}).status_code == 200
                client.post(f'jobs/{jid}/seal')
                uid = client.get(f'jobs/{jid}/entries').json()[0]['id']
                digest = hashlib.sha256(content).hexdigest()
                result = client.put(f'uploads/{uid}/data', content=content, headers={'Content-Type': 'application/octet-stream', 'Upload-Offset': '0',
                                    'Upload-Length': str(len(content)), 'Upload-SHA256': digest, 'Upload-Request-ID': 'tls'})
                assert result.status_code == 200
                client.post(f'uploads/{uid}/complete', json={'sha256': digest})
                for _ in range(100):
                    if client.get(f'uploads/{uid}').json()['state'] == 'completed':
                        break
                    time.sleep(.05)
                result = client.get(f'files/{uid}/content')
                assert result.content == content
                partial = client.get(f'files/{uid}/content', headers={'Range': 'bytes=13-101', 'If-Range': result.headers['etag']})
                assert partial.status_code == 206 and partial.content == content[13:102]
                assert client.post('jobs', json={'key': 'wrong-origin', 'name': 'wrong'}, headers={'Origin': 'https://evil.example'}).status_code == 403
        finally:
            for process in reversed(processes):
                stop_tree(process)
