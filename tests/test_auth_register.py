"""Ticket #68: register a name and receive a one-time character key.

The auth test server runs with `auth_required=True` and a real SQLite file. The
default guest server is unchanged; the v2.0 conformance session covers it.
"""

import asyncio
import base64
import json
import logging

import pytest
import websockets

from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.storage.db import open_database
from src.mud.world.loader import load_demo_village


class AuthClient:
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

    async def auth(self, data, msg_id="auth"):
        await self.send({
            "v": 2, "type": "auth", "id": msg_id, "data": data,
        })

    async def register(self, name, msg_id="register"):
        await self.auth({"mode": "register", "name": name}, msg_id)
        ok = await self.next("auth.ok")
        state = await self.next("room.state")
        return ok, state

    async def input(self, text, msg_id=1):
        await self.send({
            "v": 2, "type": "input", "id": msg_id,
            "data": {"text": text},
        })

    async def next(self, type_, timeout=3.0):
        while True:
            message = await asyncio.wait_for(self.q.get(), timeout=timeout)
            if message.get("type") == type_:
                return message

    async def next_any(self, timeout=3.0):
        return await asyncio.wait_for(self.q.get(), timeout=timeout)

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


@pytest.fixture
async def auth_server(tmp_path):
    engine = Engine()
    await load_demo_village(engine)
    instance = V2Server(
        engine, port=0, db_path=tmp_path / "auth.db", auth_required=True)
    task = asyncio.create_task(instance.run())
    await _wait_for_bind(instance)
    try:
        yield instance, engine, tmp_path / "auth.db"
    finally:
        instance.stop()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except TimeoutError:
            task.cancel()


def _key_id(key: str) -> str:
    prefix, key_id, _secret = key.split("-", 2)
    assert prefix == "mud1"
    return key_id


def _key_secret(key: str) -> bytes:
    """Decode the base64url secret so the test measures bytes, not characters."""
    secret = _key_secret_text(key)
    padded = secret + "=" * (-len(secret) % 4)
    return base64.urlsafe_b64decode(padded)


def _key_secret_text(key: str) -> str:
    return key.split("-", 2)[2]


def _stored_values(conn, table: str) -> list[str]:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return [
        str(value)
        for row in rows
        for value in tuple(row)
        if value is not None
    ]


@pytest.mark.asyncio
async def test_hello_advertises_auth_and_register_returns_auth_ok(auth_server):
    instance, _engine, _db_path = auth_server
    async with AuthClient(f"ws://localhost:{instance.port}") as ada:
        assert ada.hello["data"]["auth_required"] is True
        assert ada.hello["data"]["auth_modes"] == ["key"]

        await ada.auth(
            {"mode": "register", "name": "Ada"}, msg_id="c-1")
        ok = await ada.next("auth.ok")

        assert ok["id"] == "c-1"
        assert ok["data"]["player"] == {"name": "Ada"}
        assert ok["data"]["mode"] == "register"
        assert ok["data"]["world"]["name"] == "Starting Village"
        assert ok["data"]["world"]["epoch"]
        assert ok["data"]["text"]

        key = ok["data"]["key"]
        assert key.startswith("mud1-")
        assert len(_key_secret(key)) >= 32

        state = await ada.next("room.state")
        assert state["data"]["room"]["id"] == "gate"
        assert state["data"]["room"]["name"] == "Village Gate"

        # The key is shown exactly once: not in the room state or later traffic.
        assert "key" not in state["data"]
        assert key not in json.dumps(state)


@pytest.mark.asyncio
async def test_registered_character_and_key_are_stored_without_the_key(auth_server):
    instance, _engine, db_path = auth_server
    async with AuthClient(f"ws://localhost:{instance.port}") as ada:
        ok, _state = await ada.register("Ada")
        key = ok["data"]["key"]

    conn = open_database(db_path)
    try:
        players = conn.execute(
            "SELECT id, name, room_id, asleep FROM players").fetchall()
        assert len(players) == 1
        player = players[0]
        assert player["name"] == "Ada"
        assert player["room_id"] == "gate"
        assert player["asleep"] == 0

        keys = conn.execute(
            "SELECT key_id, player_id, salt, hash, revoked_at "
            "FROM character_keys").fetchall()
        assert len(keys) == 1
        stored = keys[0]
        assert stored["key_id"] == _key_id(key)
        assert stored["player_id"] == player["id"]
        assert stored["revoked_at"] is None
        assert len(bytes.fromhex(stored["salt"])) == 16
        assert stored["hash"].startswith("scrypt$")

        for value in _stored_values(conn, "players") + \
                _stored_values(conn, "character_keys"):
            assert key not in value
            assert _key_secret_text(key) not in value
    finally:
        conn.close()

    # Not just the columns: the plaintext key never reaches the database file.
    blobs = db_path.read_bytes()
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists():
        blobs += wal.read_bytes()
    assert key.encode() not in blobs


@pytest.mark.asyncio
async def test_auth_never_logs_the_key(auth_server, caplog):
    instance, _engine, _db_path = auth_server
    caplog.set_level(logging.DEBUG, logger="src.mud")

    async with AuthClient(f"ws://localhost:{instance.port}") as ada:
        ok, _state = await ada.register("Ada")
        assert ok["data"]["key"] not in caplog.text


@pytest.mark.asyncio
async def test_registered_client_sends_input_without_joining(auth_server):
    instance, _engine, _db_path = auth_server
    async with AuthClient(f"ws://localhost:{instance.port}") as ada:
        await ada.register("Ada")

        await ada.input("look", 2)
        state = await ada.next("room.state")

        assert state["id"] == 2
        assert state["data"]["room"]["id"] == "gate"


@pytest.mark.asyncio
async def test_duplicate_name_is_case_insensitive_and_does_not_lock_the_socket(
        auth_server):
    instance, _engine, db_path = auth_server
    uri = f"ws://localhost:{instance.port}"

    async with AuthClient(uri) as ada:
        await ada.register("Ada")

        async with AuthClient(uri) as impostor:
            await impostor.auth({"mode": "register", "name": "ada"}, "c-2")
            error = await impostor.next("auth.error")
            assert error["id"] == "c-2"
            assert error["data"]["code"] == "name_taken"

            # The name is taken, not the socket: another name still works.
            ok, _state = await impostor.register("Ben", "c-3")
            assert ok["data"]["player"]["name"] == "Ben"

    # The character is durable, so the name stays taken after a disconnect.
    async with AuthClient(uri) as late:
        await late.auth({"mode": "register", "name": "ADA"}, "c-4")
        error = await late.next("auth.error")
        assert error["data"]["code"] == "name_taken"

    conn = open_database(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM character_keys").fetchone()[0] == 2
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_invalid_names_return_invalid_name_and_keep_the_socket(auth_server):
    instance, _engine, _db_path = auth_server
    bad_names = [
        "",
        "A",
        "Ada Lovelace",
        "1ada",
        "ada!",
        "a" * 25,
        "Ada\nLovelace",
        42,
        None,
    ]
    async with AuthClient(f"ws://localhost:{instance.port}") as ada:
        for index, name in enumerate(bad_names):
            msg_id = f"bad-{index}"
            await ada.auth({"mode": "register", "name": name}, msg_id)
            error = await ada.next("auth.error")
            assert error["id"] == msg_id
            assert error["data"]["code"] == "invalid_name", name
            assert error["data"]["text"]

        # A bad attempt is not a lockout: the same socket can still register,
        # and surrounding whitespace is trimmed rather than rejected.
        ok, _state = await ada.register(" Ada-Lovelace_2 ", "good")
        assert ok["data"]["player"]["name"] == "Ada-Lovelace_2"
