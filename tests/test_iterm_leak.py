import asyncio

import pytest

pytest.importorskip("iterm2")

import iterm2  # noqa: E402

from neondeck.iterm import ItermClient  # noqa: E402


class FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeConn:
    def __init__(self):
        self.websocket = FakeWS()
        self._Connection__dispatch_forever_future = asyncio.get_running_loop().create_future()


async def test_discard_tears_down_connection_and_globals():
    iterm2.app.App.instance = object()
    iterm2.notifications._handlers["dummy"] = lambda: None
    conn = FakeConn()
    await ItermClient()._discard(conn)
    assert conn.websocket.closed
    assert conn._Connection__dispatch_forever_future.cancelled()
    assert iterm2.app.App.instance is None
    assert not iterm2.notifications._handlers


async def test_discard_none_is_safe():
    await ItermClient()._discard(None)
