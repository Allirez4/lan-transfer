import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
import time

import httpx
import pytest

from lan_transfer.desktop_controller import DesktopController, launch, stop_child
from lan_transfer.auth import issue_code


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setenv('LAN_TRANSFER_CLIENT_HOME', str(tmp_path / 'companion'))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'storage': str(tmp_path / 'storage'), 'bind': '127.0.0.1', 'port': port,
                               'hosts': ['127.0.0.1'], 'origins': [f'http://127.0.0.1:{port}'], 'safety_bytes': 0}))
    return DesktopController(path)


def test_desktop_add_refresh_remove_and_settings(controller, tmp_path):
    source = tmp_path / 'original.txt'
    source.write_text('original')
    path, sid, error = controller.add_sources([source])[0]
    assert sid and error is None
    assert controller.snapshot()['shares'][0]['status'] == 'Shared'
    source.write_text('updated')
    controller.share_action(sid, 'refresh')
    controller.share_action(sid, 'remove')
    assert controller.snapshot()['shares'][0]['status'] == 'Not shared'
    assert source.read_text() == 'updated'
    controller.save_config({'session_days': 7, 'active_files': 4})
    reloaded = DesktopController(controller.config_path)
    assert reloaded.config.session_days == 7 and reloaded.config.active_files == 4
    assert source.read_text() == 'updated'


def test_desktop_live_server_and_companion_round_trip(controller, tmp_path):
    server = controller.start_server()
    children = []
    try:
        for _ in range(150):
            if controller.health() == 'Online':
                break
            if server.poll() is not None:
                pytest.fail(server.stdout.read())
            time.sleep(.025)
        assert controller.health() == 'Online'
        origin = controller.config.origins[0]
        controller.pair_remote(origin, controller.pairing_code())
        source = tmp_path / 'original.txt'
        source.write_bytes(os.urandom(1024 * 1024 + 37))
        _, sid, error = controller.add_sources([source])[0]
        assert error is None
        assert controller.remote_shares(origin)[0]['id'] == sid
        target = tmp_path / 'downloads'
        child = launch('lan_transfer_client.main', ['download', sid, '--to', str(target), '--server', origin, '--on-conflict', 'rename'])
        children.append(child)
        output, _ = child.communicate(timeout=30)
        assert child.returncode == 0, output
        assert (target / source.name).read_bytes() == source.read_bytes()
        child = launch('lan_transfer_client.main', ['upload', str(source), '--server', origin, '--workers', '2', '--on-conflict', 'rename'])
        children.append(child)
        output, _ = child.communicate(timeout=30)
        assert child.returncode == 0, output
        assert len(controller.client_jobs()) == 2
        assert all(job['state'] == 'completed' for job in controller.client_jobs())
        assert controller.snapshot()['jobs'][0]['state'] == 'completed'
        sid = controller.snapshot()['sessions'][0]['id']
        command = controller.command('revoke', sid)
        for _ in range(150):
            completed = next((row for row in controller.snapshot()['commands'] if row['id'] == command and row['state'] == 'completed'), None)
            if completed:
                break
            time.sleep(.02)
        assert completed
        assert controller.snapshot()['sessions'][0]['status'] == 'Revoked'
        controller.command('stop')
        server.wait(timeout=15)
        assert server.returncode == 0
        assert controller.health() == 'Stopped'
    finally:
        for child in children:
            stop_child(child)
        stop_child(server)
        server.stdout.close()


def test_native_window_tabs_and_share_callback(controller, tmp_path):
    # Tcl objects must be destroyed on their creating thread. Isolate this smoke
    # test so later AnyIO worker-thread GC cannot collect the Tk widget graph.
    source = tmp_path / 'native.txt'
    source.write_text('native picker fixture')
    result = subprocess.run([sys.executable, '-c', textwrap.dedent('''
        import sys, time, threading, tkinter as tk
        from unittest.mock import patch
        from lan_transfer.desktop import DesktopApp
        from lan_transfer.desktop_controller import DesktopController
        try:
            root = tk.Tk()
        except tk.TclError:
            sys.exit(77)
        root.withdraw()
        app = DesktopApp(root, DesktopController(sys.argv[1]), auto_refresh=False)
        errors = []
        try:
            assert len(app.tabs.tabs()) == 6
            assert app.tabs.index(app.tabs.select()) == 0
            with patch('lan_transfer.desktop.messagebox.showerror', side_effect=lambda *a, **kw: errors.append(a)), patch('lan_transfer.desktop.filedialog.askopenfilenames', return_value=(sys.argv[2],)):
                app.add_files()
                for _ in range(500):
                    root.update()
                    if app.tables['shares'].get_children():
                        break
                    time.sleep(.02)
            assert not errors, errors
            assert len(app.tables['shares'].get_children()) == 1
            gate, delivered = threading.Event(), []
            app.run('Delayed launch callback', lambda: gate.wait(5), lambda _: delivered.append(True))
            app.close()
            assert app.close_requested and not app.closed
            gate.set()
            for _ in range(250):
                if app.closed:
                    break
                root.update()
                time.sleep(.02)
            assert app.closed and delivered == [True]
        finally:
            was_closed = app.closed
            app.closed = True
            app.executor.shutdown(wait=True, cancel_futures=True)
            if not was_closed:
                root.destroy()
    '''), str(controller.config_path), str(source)], capture_output=True, text=True, timeout=30)
    if result.returncode == 77:
        pytest.skip('Tk display unavailable')
    assert result.returncode == 0, result.stdout + result.stderr
    assert source.read_text() == 'native picker fixture'
    assert not list((controller.config.storage / 'received').glob('**/native.txt'))
