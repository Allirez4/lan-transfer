import asyncio

import pytest
import anyio

from lan_transfer.api import PinnedFileResponse
from lan_transfer.storage import version


def test_source_changed_during_stream_aborts(tmp_path):
    path = tmp_path / 'source.bin'
    path.write_bytes(b'a' * (2 * 1024 * 1024))
    response = PinnedFileResponse(path, version(path))
    body_messages = 0
    async def send(message):
        nonlocal body_messages
        if message['type'] == 'http.response.body' and message.get('body'):
            body_messages += 1
            with path.open('r+b') as file:
                file.seek(1024 * 1024)
                file.write(b'changed')
    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    with pytest.raises(RuntimeError, match='Source changed'):
        asyncio.run(response({'type': 'http', 'method': 'GET', 'headers': []}, receive, send))
    assert body_messages == 1


def test_download_waits_for_shared_transfer_capacity(tmp_path):
    path = tmp_path / 'limited.bin'
    path.write_bytes(b'capacity test')
    async def check():
        slots = anyio.Semaphore(1)
        await slots.acquire()  # A receiving upload currently holds the global slot.
        response = PinnedFileResponse(path, version(path), limiter=slots)
        started = anyio.Event()
        finished = anyio.Event()
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            messages.append(message)
        async def download():
            started.set()
            await response({'type': 'http', 'method': 'GET', 'headers': []}, receive, send)
            finished.set()
        async with anyio.create_task_group() as group:
            group.start_soon(download)
            await started.wait()
            await anyio.sleep(.02)
            assert not messages
            slots.release()
            with anyio.fail_after(2):
                await finished.wait()
        assert b''.join(m.get('body', b'') for m in messages) == b'capacity test'
        assert slots.value == 1
    anyio.run(check)
