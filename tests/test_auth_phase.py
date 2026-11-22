"""Ticket #74: the auth phase gates join and input.

A v2.1 server requires authentication by default. A client that sends `join` or
`input` before authenticating gets `error{not_authenticated}` and the socket
stays open; the same connection can then register or log in. `hello` advertises
the phase through `auth_required` and `auth_modes`.

The v2.0 guest path remains available, but only for a caller that passes
`auth_required=False` explicitly (legacy tests, a deliberate guest server).
"""

import asyncio
import json
from pathlib import Path

import pytest
import websockets

from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.world.loader import load_world

WORLD_PATH = (
    Path(__file__).resolve().parents[1]
    / "data" / "worlds" / "starting_village.yaml"
)


class PhaseClient:
    """A tiny v2 client with a background reader and type-filtered drain."""

    def __init__(self, uri):
        self.uri = uri
        self.ws = None
        self.q = asyncio.Queue()
        self.task = None
        self.hello = None

    async def connect(self):
        self.ws = await websockets.connect(self.uri)
        self.hello = json.loads(await self.ws.recv())
        self.task = asyncio.create_task(self._reader())
        return self

    async def _reader(self):
        try:
            async for raw in self.ws:
                self.q.put_nowait(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, *exc):
        await self.close()

    async def send(self, message):
        await self.ws.send(json.dumps(message))

    async def input(self, text, msg_id=1):
        await self.send({
            "v": 2, "type": "input", "id": msg_id,
            "data": {"text": text},
        })

    async def register(self, name, msg_id="register"):
        await self.send({
            "v": 2, "type": "auth", "id": msg_id,
            "data": {"mode": "register", "name": name},
        })
        ok = await self.next("auth.ok")
        state = await self.next("room.state")
        return ok, state

    async def next(self, type_, timeout=3.0):
        while True:
            message = await asyncio.wait_for(self.q.get(), timeout=timeout)
            if message.get("type") == type_:
                return message

    async def close(self):
        if self.task is not None:
            self.task.cancel()
        if self.ws is not None:
            await self.ws.close()


async def _wait_for_bind(server: V2Server) -> None:
    for _ in range(200):
        if server.port:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("v2 server did not bind")


async def _stop(server: V2Server, task) -> None:
    server.stop()
    try:
        await asyncio.wait_for(task, timeout=3.0)
    except TimeoutError:
        task.cancel()


@pytest.fixture
async def server(tmp_path):
    """The shipped default: a database-backed server that requires auth."""
    engine = Engine()
    await load_world(engine, WORLD_PATH)
    instance = V2Server(engine, port=0, db_path=tmp_path / "phase.db")
    task = asyncio.create_task(instance.run())
    await _wait_for_bind(instance)
    try:
        yield instance, engine
    finally:
        await _stop(instance, task)


@pytest.mark.asyncio
async def test_hello_advertises_the_auth_phase(server):
    instance, _engine = server
    async with PhaseClient(f"ws://localhost:{instance.port}") as ada:
        assert ada.hello["v"] == 2
        assert ada.hello["type"] == "hello"
        assert ada.hello["data"]["protocol"] == [2]
        assert ada.hello["data"]["auth_required"] is True
        assert ada.hello["data"]["auth_modes"] == ["key"]


@pytest.mark.asyncio
async def test_join_before_auth_is_not_authenticated_and_the_socket_stays_open(
        server):
    instance, _engine = server
    async with PhaseClient(f"ws://localhost:{instance.port}") as ada:
        await ada.send({
            "v": 2, "type": "join", "id": "early",
            "data": {"name": "Ada"},
        })
        error = await ada.next("error")

        assert error["id"] == "early"
        assert error["data"]["code"] == "not_authenticated"
        assert error["data"]["message"]
        assert ada.ws.close_code is None

        # The rejected connection is still usable: auth then works on it.
        ok, state = await ada.register("Ada")
        assert ok["data"]["player"] == {"name": "Ada"}
        assert ok["data"]["mode"] == "register"
        assert ok["data"]["key"].startswith("mud1-")
        assert state["data"]["room"]["id"] == "gate"


@pytest.mark.asyncio
async def test_input_before_auth_is_not_authenticated_and_the_socket_stays_open(
        server):
    instance, _engine = server
    async with PhaseClient(f"ws://localhost:{instance.port}") as ada:
        await ada.input("look", 7)
        error = await ada.next("error")

        assert error["id"] == 7
        assert error["data"]["code"] == "not_authenticated"
        assert ada.ws.close_code is None

        await ada.register("Ada")

        await ada.input("look", 8)
        state = await ada.next("room.state")
        assert state["id"] == 8
        assert state["data"]["room"]["id"] == "gate"


@pytest.mark.asyncio
async def test_auth_is_the_default_and_guest_is_an_explicit_opt_out(tmp_path):
    default = V2Server(Engine(), port=0)
    assert default.auth_required is True

    engine = Engine()
    await load_world(engine, WORLD_PATH)
    guest = V2Server(
        engine, port=0, db_path=tmp_path / "guest.db", auth_required=False)
    task = asyncio.create_task(guest.run())
    await _wait_for_bind(guest)
    try:
        async with PhaseClient(f"ws://localhost:{guest.port}") as ada:
            assert ada.hello["data"]["auth_required"] is False
            assert "auth_modes" not in ada.hello["data"]

            # The guest server keeps the v2.0 error for input before join.
            await ada.input("look", 1)
            error = await ada.next("error")
            assert error["data"]["code"] == "not_joined"

            await ada.send({
                "v": 2, "type": "join", "id": "j",
                "data": {"name": "Ada"},
            })
            ok = await ada.next("session.ok")
            assert ok["data"]["mode"] == "guest"
            state = await ada.next("room.state")
            assert state["data"]["room"]["id"] == "gate"
    finally:
        await _stop(guest, task)
