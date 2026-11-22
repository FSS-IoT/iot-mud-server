"""Ticket #70: typed auth errors and a per-connection auth-attempt limiter.

The acceptance for v2.1.1 criterion 3 is three typed failures plus a limit that
never locks a name:

* an invalid key is `auth.error{invalid_key}`;
* a duplicate name is `auth.error{name_taken}`;
* a bad name is `auth.error{invalid_name}`;
* after the configured failures inside the window the next `auth` is
  `auth.error{rate_limited}`, the socket stays open, and the limit clears when
  the window passes;
* the limiter is keyed by the connection, so a different connection can still
  register or log in while one connection is throttled, and no name lock exists
  in memory or in SQLite.

The clock is injected. Nothing here sleeps for the window.
"""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
import websockets

from src.mud.config import AuthConfig, ConfigError, load_config
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.storage.db import open_database
from src.mud.world.loader import load_demo_village

# The shipped default the server uses when no `[auth]` table is present.
DEFAULT_MAX_ATTEMPTS = AuthConfig().max_attempts


class Clock:
    """A manual monotonic clock: the test advances time, never sleeps."""

    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


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
        return await self.next("auth.error")

    async def auth_reply(self, data, msg_id="auth"):
        await self.send({
            "v": 2, "type": "auth", "id": msg_id, "data": data,
        })
        return await self.next_any()

    async def register(self, name, msg_id="register"):
        reply = await self.auth_reply(
            {"mode": "register", "name": name}, msg_id)
        assert reply["type"] == "auth.ok", reply["data"]
        await self.next("room.state")
        return reply

    async def login(self, key, msg_id="login"):
        reply = await self.auth_reply({"mode": "login", "key": key}, msg_id)
        assert reply["type"] == "auth.ok", reply["data"]
        await self.next("room.state")
        return reply

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


async def _stop(server: V2Server, task) -> None:
    server.stop()
    try:
        await asyncio.wait_for(task, timeout=3.0)
    except TimeoutError:
        task.cancel()


@asynccontextmanager
async def auth_server(tmp_path, **server_kwargs):
    engine = Engine()
    await load_demo_village(engine)
    instance = V2Server(
        engine, port=0, db_path=tmp_path / "auth-errors.db",
        auth_required=True, **server_kwargs)
    task = asyncio.create_task(instance.run())
    await _wait_for_bind(instance)
    try:
        yield instance, engine
    finally:
        await _stop(instance, task)


async def _register_ada(instance):
    """Register Ada on her own connection and return her key."""
    async with AuthClient(f"ws://localhost:{instance.port}") as ada:
        reply = await ada.register("Ada", "setup")
        return reply["data"]["key"]


@pytest.fixture
async def server(tmp_path):
    async with auth_server(tmp_path) as (instance, engine):
        yield instance, engine


@pytest.mark.asyncio
async def test_invalid_key_duplicate_name_and_bad_name_are_typed_errors(server):
    instance, _engine = server
    uri = f"ws://localhost:{instance.port}"
    await _register_ada(instance)

    async with AuthClient(uri) as client:
        invalid = await client.auth(
            {"mode": "login", "key": "mud1-k0011223344556677-nope"}, "e-1")
        assert invalid["id"] == "e-1"
        assert invalid["data"]["code"] == "invalid_key"
        assert invalid["data"]["text"]
        assert client.ws.close_code is None

        duplicate = await client.auth(
            {"mode": "register", "name": "Ada"}, "e-2")
        assert duplicate["id"] == "e-2"
        assert duplicate["data"]["code"] == "name_taken"
        assert client.ws.close_code is None

        bad_name = await client.auth(
            {"mode": "register", "name": "1ada"}, "e-3")
        assert bad_name["id"] == "e-3"
        assert bad_name["data"]["code"] == "invalid_name"
        assert client.ws.close_code is None


@pytest.mark.asyncio
async def test_failures_are_counted_and_the_next_attempt_is_rate_limited(server):
    """A bad key is typed; a flood is typed `rate_limited`, not a close."""
    instance, _engine = server
    key = await _register_ada(instance)

    async with AuthClient(f"ws://localhost:{instance.port}") as flood:
        for index in range(DEFAULT_MAX_ATTEMPTS):
            error = await flood.auth(
                {"mode": "login", "key": "mud1-k0011223344556677-nope"},
                f"bad-{index}")
            assert error["data"]["code"] == "invalid_key", error["data"]
            assert flood.ws.close_code is None

        limited = await flood.auth_reply(
            {"mode": "login", "key": key}, "limited")
        assert limited["type"] == "auth.error", limited
        assert limited["id"] == "limited"
        assert limited["data"]["code"] == "rate_limited"
        assert limited["data"]["text"]
        # The socket is not dropped for a normal bad key or for the limit.
        assert flood.ws.close_code is None


@pytest.mark.asyncio
async def test_the_limiter_is_per_connection_not_per_name(server):
    """One throttled connection must not lock Ada's name or key."""
    instance, _engine = server
    key = await _register_ada(instance)
    uri = f"ws://localhost:{instance.port}"

    async with AuthClient(uri) as flood:
        for index in range(DEFAULT_MAX_ATTEMPTS):
            await flood.auth(
                {"mode": "login", "key": "nope"}, f"bad-{index}")
        limited = await flood.auth_reply(
            {"mode": "login", "key": key}, "flood")
        assert limited["type"] == "auth.error", limited
        assert limited["data"]["code"] == "rate_limited"

        # A different connection is unaffected: Ada's key still works and her
        # name is not locked.
        async with AuthClient(uri) as other:
            ok = await other.login(key, "other")
            assert ok["data"]["player"] == {"name": "Ada"}


@pytest.mark.asyncio
async def test_no_name_lock_table_or_row_exists(server, tmp_path):
    instance, _engine = server
    uri = f"ws://localhost:{instance.port}"
    key = await _register_ada(instance)

    async with AuthClient(uri) as flood:
        for index in range(DEFAULT_MAX_ATTEMPTS):
            await flood.auth(
                {"mode": "login", "key": "nope"}, f"bad-{index}")

        # The flood is local to this connection; the durable state holds only
        # characters and keys, no lock or attempt table.
        conn = open_database(tmp_path / "auth-errors.db")
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            conn.close()
        assert not any(
            word in table.lower()
            for table in tables
            for word in ("attempt", "lock", "throttle", "rate")
        ), tables

        # The name can still be used from a fresh connection after the flood.
        async with AuthClient(uri) as other:
            await other.login(key, "late")


@pytest.mark.asyncio
async def test_mixed_typed_failures_count_toward_the_limit(tmp_path):
    """Bad names and taken names count too, not only bad keys."""
    clock = Clock()
    limits = AuthConfig(max_attempts=3, window_seconds=30.0)
    async with auth_server(
            tmp_path, auth_limits=limits, clock=clock) as (instance, _engine):
        key = await _register_ada(instance)

        async with AuthClient(f"ws://localhost:{instance.port}") as client:
            first = await client.auth(
                {"mode": "register", "name": "1ada"}, "f-1")
            second = await client.auth(
                {"mode": "login", "key": "nope"}, "f-2")
            third = await client.auth(
                {"mode": "register", "name": "Ada"}, "f-3")
            assert [first["data"]["code"], second["data"]["code"],
                    third["data"]["code"]] == [
                "invalid_name", "invalid_key", "name_taken"]

            limited = await client.auth_reply(
                {"mode": "login", "key": key}, "f-4")
            assert limited["type"] == "auth.error", limited
            assert limited["data"]["code"] == "rate_limited"
            assert client.ws.close_code is None


@pytest.mark.asyncio
async def test_the_limit_is_configurable_and_the_injected_clock_recovers(
        tmp_path):
    """The window is measured on the injected clock, never by sleeping."""
    clock = Clock()
    limits = AuthConfig(max_attempts=2, window_seconds=30.0)
    async with auth_server(
            tmp_path, auth_limits=limits, clock=clock) as (instance, _engine):
        key = await _register_ada(instance)

        async with AuthClient(f"ws://localhost:{instance.port}") as client:
            for index in range(2):
                error = await client.auth(
                    {"mode": "login", "key": "nope"}, f"bad-{index}")
                assert error["data"]["code"] == "invalid_key"

            limited = await client.auth_reply(
                {"mode": "login", "key": key}, "early")
            assert limited["type"] == "auth.error", limited
            assert limited["data"]["code"] == "rate_limited"

            # Almost a full window later it is still limited: only the injected
            # clock moved, and it has not moved far enough.
            clock.advance(29.0)
            still = await client.auth_reply(
                {"mode": "login", "key": key}, "still")
            assert still["data"]["code"] == "rate_limited"

            # Past the window the limiter forgets the failures, and the same
            # socket can authenticate.
            clock.advance(2.0)
            ok = await client.auth_reply(
                {"mode": "login", "key": key}, "recovered")
            assert ok["type"] == "auth.ok", ok
            await client.next("room.state")
            assert client.ws.close_code is None


@pytest.mark.asyncio
async def test_a_valid_key_from_another_connection_works_while_one_is_limited(
        tmp_path):
    """The limiter is per connection: a throttle never becomes a name lock."""
    clock = Clock()
    limits = AuthConfig(max_attempts=2, window_seconds=60.0)
    async with auth_server(
            tmp_path, auth_limits=limits, clock=clock) as (instance, _engine):
        key = await _register_ada(instance)
        uri = f"ws://localhost:{instance.port}"

        async with AuthClient(uri) as flood:
            for index in range(2):
                await flood.auth(
                    {"mode": "login", "key": "nope"}, f"bad-{index}")
            assert (await flood.auth_reply(
                {"mode": "login", "key": key}, "flood")
            )["data"]["code"] == "rate_limited"

            async with AuthClient(uri) as clean:
                ok = await clean.login(key, "clean")
                assert ok["data"]["player"] == {"name": "Ada"}
                assert clean.ws.close_code is None

            # The clean login does not clear or extend the flood's window.
            assert (await flood.auth_reply(
                {"mode": "login", "key": key}, "flood-2")
            )["data"]["code"] == "rate_limited"


def test_auth_config_defaults_without_a_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.auth.max_attempts == 20
    assert config.auth.window_seconds == 60.0


def test_auth_config_is_read_from_mud_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mud.toml").write_text(
        "[auth]\nmax_attempts = 3\nwindow_seconds = 2.5\n",
        encoding="utf-8",
    )

    config = load_config()

    assert config.auth.max_attempts == 3
    assert config.auth.window_seconds == 2.5


@pytest.mark.parametrize("body", [
    "[auth]\nmax_attempts = 0\n",
    "[auth]\nmax_attempts = -1\n",
    "[auth]\nmax_attempts = true\n",
    '[auth]\nmax_attempts = "5"\n',
    "[auth]\nwindow_seconds = 0\n",
    "[auth]\nwindow_seconds = -1\n",
    '[auth]\nwindow_seconds = "60"\n',
    "[auth]\nwindow_seconds = inf\n",
])
def test_auth_config_rejects_unsafe_values(tmp_path, body):
    path = tmp_path / "bad.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ConfigError, match="auth"):
        load_config(path)


def test_auth_must_be_a_table(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('auth = "not a table"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="auth"):
        load_config(path)
