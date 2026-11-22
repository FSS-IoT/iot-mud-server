"""Regression: simultaneous disconnects must not fail the room broadcast.

When several authenticated clients close at once, one handler can be in
`_cleanup` and broadcast `room.leave` to a socket another handler has already
closed. `_broadcast_room` used to let `websockets.exceptions.ConnectionClosed`
escape, so the websockets server logged `connection handler failed` for a
normal disconnect. #89 requires no unhandled exceptions in the playtest.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.world.loader import load_demo_village
from tools.load_smoke import LoadClient, client_names, load_limits

CLIENTS = 20


@pytest.fixture
async def server(tmp_path):
    engine = Engine()
    await load_demo_village(engine)
    instance = V2Server(
        engine,
        port=0,
        db_path=tmp_path / "cleanup.db",
        auth_required=True,
        limits=load_limits(),
        server_commit="0" * 40,
        idle_poll_seconds=3600.0,
    )
    task = asyncio.create_task(instance.run())
    for _ in range(500):
        if instance.port:
            break
        await asyncio.sleep(0.01)
    assert instance.port, "v2 server did not bind"
    try:
        yield instance
    finally:
        instance.stop()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except TimeoutError:
            task.cancel()


@pytest.mark.asyncio
async def test_simultaneous_disconnects_do_not_log_connection_handler_failed(
        server, caplog):
    """Twenty clients in one room close together; cleanup may race a push."""
    caplog.set_level(logging.ERROR, logger="websockets.server")
    uri = f"ws://localhost:{server.port}"
    clients = [
        LoadClient(uri, name, []) for name in client_names(CLIENTS)
    ]
    await asyncio.gather(*(client.connect() for client in clients))
    await asyncio.gather(*(client.register() for client in clients))

    # No commands are needed: every client is awake in the gate, so each
    # cleanup broadcasts room.leave to the other 19.
    await asyncio.gather(*(client.close() for client in clients))

    for _ in range(200):
        if not server._sessions:
            break
        await asyncio.sleep(0.01)
    assert not server._sessions, "a connection handler never finished cleanup"

    failures = [
        record
        for record in caplog.records
        if record.name == "websockets.server"
        and "connection handler failed" in record.getMessage()
    ]
    assert failures == [], [record.getMessage() for record in failures]
