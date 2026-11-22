"""Ticket #69: log in with a character key.

The auth test server runs with `auth_required=True` and a real SQLite file, so a
registration survives a disconnect and a restart. The default guest server is
unchanged and covered by `test_protocol_v2.py`; #74 flips the default.

The login request carries no name. The public key id selects the row and the
secret is verified against the stored scrypt hash with a constant-time compare,
so a failed login never says whether a character name exists.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

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

    async def auth_reply(self, data, msg_id="auth"):
        """Send an auth message and return its first reply, whatever it is."""
        await self.auth(data, msg_id)
        return await self.next_any()

    async def register(self, name, msg_id="register"):
        reply = await self.auth_reply(
            {"mode": "register", "name": name}, msg_id)
        assert reply["type"] == "auth.ok", reply["data"]
        state = await self.next("room.state")
        return reply, state

    async def login(self, key, msg_id="login"):
        reply = await self.auth_reply({"mode": "login", "key": key}, msg_id)
        assert reply["type"] == "auth.ok", reply["data"]
        state = await self.next("room.state")
        return reply, state

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


async def _start(db_path):
    engine = Engine()
    await load_demo_village(engine)
    instance = V2Server(
        engine, port=0, db_path=db_path, auth_required=True)
    task = asyncio.create_task(instance.run())
    await _wait_for_bind(instance)
    return instance, engine, task


async def _stop(instance, task):
    instance.stop()
    try:
        await asyncio.wait_for(task, timeout=3.0)
    except TimeoutError:
        task.cancel()


@asynccontextmanager
async def auth_server(db_path):
    instance, engine, task = await _start(db_path)
    try:
        yield instance, engine
    finally:
        await _stop(instance, task)


@pytest.fixture
async def server(tmp_path):
    async with auth_server(tmp_path / "auth.db") as (instance, engine):
        yield instance, engine, tmp_path / "auth.db"


def _key_id(key: str) -> str:
    return key.split("-", 2)[1]


def _tamper(key: str) -> str:
    """A key with the same id and a different secret."""
    replacement = "A" if key[-1] != "A" else "B"
    return key[:-1] + replacement


@pytest.mark.asyncio
async def test_login_returns_auth_ok_without_the_key_then_room_state(server):
    instance, _engine, _db_path = server
    uri = f"ws://localhost:{instance.port}"

    async with AuthClient(uri) as ada:
        registered, _state = await ada.register("Ada", "c-1")
        key = registered["data"]["key"]

    async with AuthClient(uri) as ada:
        ok, state = await ada.login(key, "c-2")

        assert ok["id"] == "c-2"
        assert ok["data"]["player"] == {"name": "Ada"}
        assert ok["data"]["mode"] == "login"
        assert ok["data"]["world"]["name"] == "Starting Village"
        assert ok["data"]["world"]["epoch"]
        assert ok["data"]["text"]
        assert "key" not in ok["data"]
        assert key not in json.dumps(ok)

        assert state["data"]["room"]["id"] == "gate"
        assert state["data"]["room"]["name"] == "Village Gate"
        assert key not in json.dumps(state)

        # The session is bound: commands work without another auth message.
        await ada.input("look", 3)
        looked = await ada.next("room.state")
        assert looked["id"] == 3
        assert looked["data"]["room"]["id"] == "gate"


@pytest.mark.asyncio
async def test_login_uses_only_the_key_and_ignores_any_name(server):
    instance, _engine, _db_path = server
    uri = f"ws://localhost:{instance.port}"

    async with AuthClient(uri) as ada:
        registered, _state = await ada.register("Ada", "c-1")
        ada_key = registered["data"]["key"]
    async with AuthClient(uri) as ben:
        registered, _state = await ben.register("Ben", "c-2")
        assert registered["data"]["player"]["name"] == "Ben"

    # The request carries a name that exists, but login must not consult it:
    # the key alone decides who the session becomes.
    async with AuthClient(uri) as client:
        reply = await client.auth_reply({
            "mode": "login",
            "key": ada_key,
            "name": "Ben",
        }, "c-3")
        assert reply["type"] == "auth.ok", reply
        assert reply["data"]["player"] == {"name": "Ada"}
        await client.next("room.state")


@pytest.mark.asyncio
async def test_wrong_secret_unknown_key_id_and_malformed_key_are_invalid_key(
        server):
    instance, _engine, _db_path = server
    uri = f"ws://localhost:{instance.port}"

    async with AuthClient(uri) as ada:
        registered, _state = await ada.register("Ada", "c-1")
        key = registered["data"]["key"]

    async with AuthClient(uri) as client:
        wrong_secret = await client.auth_reply(
            {"mode": "login", "key": _tamper(key)}, "c-2")
        assert wrong_secret["type"] == "auth.error", wrong_secret
        assert wrong_secret["id"] == "c-2"
        assert wrong_secret["data"]["code"] == "invalid_key"
        assert wrong_secret["data"]["text"]
        assert key not in json.dumps(wrong_secret)
        message = wrong_secret["data"]["message"]

        unknown_id = "mud1-k0011223344556677-" + key.split("-", 2)[2]
        unknown = await client.auth_reply(
            {"mode": "login", "key": unknown_id}, "c-3")
        assert unknown["type"] == "auth.error", unknown
        assert unknown["data"]["code"] == "invalid_key"
        # The same message for an unknown key id and a bad secret: the reply
        # does not say which half failed.
        assert unknown["data"]["message"] == message

        malformed_keys = [
            "",
            "nope",
            "mud1",
            "mud1-",
            "mud1--secret",
            "mud1-k0123456789abcdef",
            "mud1-0123456789abcdef-secret",
            "mud1-k0123456789abcde-secret",
            None,
            42,
        ]
        for index, bad_key in enumerate(malformed_keys):
            msg_id = f"bad-{index}"
            error = await client.auth_reply(
                {"mode": "login", "key": bad_key}, msg_id)
            assert error["type"] == "auth.error", (bad_key, error)
            assert error["id"] == msg_id
            assert error["data"]["code"] == "invalid_key", bad_key

        # A bad key is not a lockout: the same socket can still log in.
        ok, _state = await client.login(key, "c-4")
        assert ok["data"]["player"]["name"] == "Ada"


@pytest.mark.asyncio
async def test_revoked_key_is_invalid_key_with_the_same_message(server):
    instance, _engine, db_path = server
    uri = f"ws://localhost:{instance.port}"

    async with AuthClient(uri) as ada:
        registered, _state = await ada.register("Ada", "c-1")
        key = registered["data"]["key"]

    conn = open_database(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE character_keys SET revoked_at = ? WHERE key_id = ?",
                ("2026-11-21T00:00:00Z", _key_id(key)),
            )
    finally:
        conn.close()

    async with AuthClient(uri) as ada:
        revoked = await ada.auth_reply(
            {"mode": "login", "key": key}, "c-2")
        assert revoked["type"] == "auth.error", revoked
        assert revoked["data"]["code"] == "invalid_key"

        wrong = await ada.auth_reply(
            {"mode": "login", "key": _tamper(key)}, "c-3")
        assert wrong["data"]["message"] == revoked["data"]["message"]


@pytest.mark.asyncio
async def test_login_while_the_character_is_already_in_play_is_name_taken(
        server):
    instance, _engine, _db_path = server
    uri = f"ws://localhost:{instance.port}"

    async with AuthClient(uri) as ada:
        registered, _state = await ada.register("Ada", "c-1")
        key = registered["data"]["key"]

        async with AuthClient(uri) as second:
            reply = await second.auth_reply(
                {"mode": "login", "key": key}, "c-2")
            assert reply["type"] == "auth.error", reply
            assert reply["data"]["code"] == "name_taken"

        # The first session is unaffected; the rejected one is not bound.
        await ada.input("look", 2)
        assert (await ada.next("room.state"))["id"] == 2

    # Once the character disconnects, the same key logs in normally.
    async with AuthClient(uri) as ada:
        ok, _state = await ada.login(key, "c-3")
        assert ok["data"]["player"]["name"] == "Ada"


@pytest.mark.asyncio
async def test_login_resumes_the_stored_room_after_a_restart(tmp_path):
    db_path = tmp_path / "restart.db"

    async with auth_server(db_path) as (instance, _engine), \
            AuthClient(f"ws://localhost:{instance.port}") as ada:
        registered, _state = await ada.register("Ada", "c-1")
        key = registered["data"]["key"]

        await ada.input("go north", 2)
        moved = await ada.next("room.state")
        assert moved["data"]["room"]["id"] == "market"

    # A hard restart: a new engine, a new server, the same database.
    async with auth_server(db_path) as (instance, _engine), \
            AuthClient(f"ws://localhost:{instance.port}") as ada:
        ok, state = await ada.login(key, "c-2")
        assert ok["data"]["player"]["name"] == "Ada"
        assert state["data"]["room"]["id"] == "market"
        assert state["data"]["room"]["name"] == "Marketplace"


@pytest.mark.asyncio
async def test_login_never_logs_the_key(server, caplog):
    instance, _engine, _db_path = server
    uri = f"ws://localhost:{instance.port}"
    caplog.set_level(logging.DEBUG, logger="src.mud")

    async with AuthClient(uri) as ada:
        registered, _state = await ada.register("Ada", "c-1")
        key = registered["data"]["key"]

    async with AuthClient(uri) as ada:
        await ada.auth_reply(
            {"mode": "login", "key": _tamper(key)}, "c-2")
        await ada.login(key, "c-3")

    assert key not in caplog.text
    assert key.split("-", 2)[2] not in caplog.text


@pytest.mark.asyncio
async def test_auth_stays_opt_in_and_the_guest_path_is_unchanged(tmp_path):
    engine = Engine()
    await load_demo_village(engine)
    instance = V2Server(engine, port=0)
    task = asyncio.create_task(instance.run())
    await _wait_for_bind(instance)
    try:
        assert instance.auth_required is False
        async with AuthClient(f"ws://localhost:{instance.port}") as ada:
            assert ada.hello["data"]["auth_required"] is False
            assert "auth_modes" not in ada.hello["data"]
            await ada.send({
                "v": 2, "type": "join", "id": "j",
                "data": {"name": "Ada"},
            })
            ok = await ada.next("session.ok")
            assert ok["data"]["mode"] == "guest"
            state = await ada.next("room.state")
            assert state["data"]["room"]["id"] == "gate"
    finally:
        await _stop(instance, task)
