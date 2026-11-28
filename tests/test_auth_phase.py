"""Ticket #74: the auth phase offers key auth and a guest fallback.

A v2.1 server offers key auth by default (`hello.auth_required` is true).
A client that sends `join` becomes a guest when `hello.guest_ok` is true (the
default), so a v2.0 client still works. A server with `guest_ok=False` rejects
`join` with `error{not_authenticated}` and keeps the socket open; the same
connection can then register or log in. `input` before any join is
`not_joined`.
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
        data = ada.hello["data"]
        assert data["protocol"] == [2]
        assert data["server_version"]
        assert data["feature_level"] == 1
        assert data["capabilities"]
        assert data["auth_required"] is True
        assert data["guest_ok"] is True
        assert data["auth_modes"] == ["key"]


@pytest.mark.asyncio
async def test_join_is_a_guest_when_guest_ok_is_true(server):
    instance, _engine = server
    async with PhaseClient(f"ws://localhost:{instance.port}") as ada:
        await ada.send({
            "v": 2, "type": "join", "id": "early",
            "data": {"name": "Ada"},
        })
        ok = await ada.next("session.ok")
        assert ok["data"]["mode"] == "guest"
        assert ok["data"]["player"] == {"name": "Ada"}
        state = await ada.next("room.state")
        assert state["data"]["room"]["id"] == "gate"
        assert ada.ws.close_code is None


@pytest.mark.asyncio
async def test_a_guest_spills_its_inventory_when_it_leaves(server):
    instance, _engine = server
    async with (
        PhaseClient(f"ws://localhost:{instance.port}") as ada,
        PhaseClient(f"ws://localhost:{instance.port}") as ben,
    ):
        for who in (ada, ben):
            await who.send({
                "v": 2, "type": "join", "id": "join",
                "data": {"name": "Ada" if who is ada else "Ben"},
            })
            await who.next("session.ok")
            await who.next("room.state")

        await ada.input("go north", 1)
        await ada.next("room.state")
        await ada.input("take gold coin", 2)
        taken = await ada.next("object.taken")
        assert taken["data"]["object"]["name"] == "Gold Coin"

        await ben.input("go north", 3)
        await ben.next("room.state")

        await ada.close()
        dropped = await ben.next("object.dropped")
        assert dropped["data"]["by"] == "Ada"
        assert dropped["data"]["object"]["name"] == "Gold Coin"
        assert dropped["data"]["self"] is False

        await ben.input("look", 4)
        state = await ben.next("room.state")
        names = [obj["name"] for obj in state["data"]["room"]["objects"]]
        assert "Gold Coin" in names


@pytest.mark.asyncio
async def test_input_before_join_is_not_joined_and_the_socket_stays_open(
        server):
    instance, _engine = server
    async with PhaseClient(f"ws://localhost:{instance.port}") as ada:
        await ada.input("look", 7)
        error = await ada.next("error")

        assert error["id"] == 7
        assert error["data"]["code"] == "not_joined"
        assert ada.ws.close_code is None

        await ada.register("Ada")

        await ada.input("look", 8)
        state = await ada.next("room.state")
        assert state["id"] == 8
        assert state["data"]["room"]["id"] == "gate"


@pytest.mark.asyncio
async def test_guest_is_the_fallback_and_can_be_disabled(tmp_path):
    default = V2Server(Engine(), port=0)
    assert default.auth_required is True
    assert default.guest_ok is True

    engine = Engine()
    await load_world(engine, WORLD_PATH)
    strict = V2Server(
        engine, port=0, db_path=tmp_path / "strict.db", guest_ok=False)
    task = asyncio.create_task(strict.run())
    await _wait_for_bind(strict)
    try:
        async with PhaseClient(f"ws://localhost:{strict.port}") as ada:
            assert ada.hello["data"]["auth_required"] is True
            assert ada.hello["data"]["guest_ok"] is False

            await ada.send({
                "v": 2, "type": "join", "id": "early",
                "data": {"name": "Ada"},
            })
            error = await ada.next("error")
            assert error["id"] == "early"
            assert error["data"]["code"] == "not_authenticated"
            assert ada.ws.close_code is None

            # The refused connection is still usable: auth then works.
            ok, state = await ada.register("Ada")
            assert ok["data"]["player"] == {"name": "Ada"}
            assert ok["data"]["mode"] == "register"
            assert state["data"]["room"]["id"] == "gate"
    finally:
        await _stop(strict, task)


@pytest.mark.asyncio
async def test_a_guest_only_server_still_advertises_guest_ok(tmp_path):
    engine = Engine()
    await load_world(engine, WORLD_PATH)
    guest = V2Server(
        engine, port=0, db_path=tmp_path / "guest.db", auth_required=False)
    task = asyncio.create_task(guest.run())
    await _wait_for_bind(guest)
    try:
        async with PhaseClient(f"ws://localhost:{guest.port}") as ada:
            assert ada.hello["data"]["auth_required"] is False
            assert ada.hello["data"]["guest_ok"] is True
            assert "auth_modes" not in ada.hello["data"]

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
