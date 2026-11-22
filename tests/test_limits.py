"""Ticket #85: configurable command limits and the idle timeout.

SPEC v2.1.4 criterion 1, with the owner's idle default from the open question:

* The command budget is a per-connection token bucket, separate from the
  per-connection auth-failure limiter (#70). A command over the burst gets a
  direct `error{rate_limited}` that echoes the request id; the socket stays
  open, and the bucket refills on the injected clock.
* An authenticated client that sends nothing for `idle_timeout_seconds` is put
  to sleep through the #72 path: the character stays in its room as
  asleep/stone, `who` shows it, and the socket closes with `4004`.
* The defaults live in `[limits]`; the idle default is 15 minutes.

The clock is injected and the idle monitor is not polled in these tests, so
nothing here sleeps for a limit.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Self

import pytest
import websockets

from src.mud.config import ConfigError, LimitsConfig, load_config
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.storage.keys import room_key
from src.mud.world.loader import load_demo_village

IDLE_TIMEOUT_CLOSE = 4004


class Clock:
    """A manual monotonic clock: the test advances time, never sleeps."""

    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Client:
    """A tiny v2 client with a background reader and a typed drain."""

    def __init__(self, uri: str):
        self.uri = uri
        self.ws = None
        self.q: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.hello = None

    async def connect(self) -> Self:
        self.ws = await websockets.connect(self.uri)
        self.hello = json.loads(await self.ws.recv())
        self.task = asyncio.create_task(self._reader())
        return self

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                self.q.put_nowait(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send(self, message: dict) -> None:
        await self.ws.send(json.dumps(message))

    async def register(self, name: str, msg_id="register") -> str:
        await self.send({
            "v": 2, "type": "auth", "id": msg_id,
            "data": {"mode": "register", "name": name},
        })
        ok = await self.next("auth.ok", msg_id=msg_id)
        await self.next("room.state")
        return ok["data"]["key"]

    async def login(self, key: str, msg_id="login"):
        await self.send({
            "v": 2, "type": "auth", "id": msg_id,
            "data": {"mode": "login", "key": key},
        })
        ok = await self.next("auth.ok", msg_id=msg_id)
        state = await self.next("room.state")
        return ok, state

    async def input(self, text: str, msg_id=1) -> None:
        await self.send({
            "v": 2, "type": "input", "id": msg_id,
            "data": {"text": text},
        })

    async def who(self, msg_id="who") -> dict:
        await self.input("who", msg_id)
        return await self.next("who.list", msg_id=msg_id)

    async def next(self, type_: str, *, msg_id=None, timeout: float = 5.0):
        while True:
            message = await asyncio.wait_for(self.q.get(), timeout)
            if message.get("type") != type_:
                continue
            if msg_id is not None and message.get("id") != msg_id:
                continue
            return message

    async def next_any(self, timeout: float = 5.0):
        return await asyncio.wait_for(self.q.get(), timeout)

    async def close_code(self, timeout: float = 5.0) -> int | None:
        try:
            await asyncio.wait_for(self.ws.wait_closed(), timeout)
        except websockets.exceptions.ConnectionClosed:
            # A 4004 close is "abnormal" to the client API but is the
            # documented idle outcome; the code is still on the connection.
            pass
        return self.ws.close_code

    async def close(self) -> None:
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
async def limits_server(tmp_path, *, limits: LimitsConfig, clock: Clock):
    engine = Engine()
    await load_demo_village(engine)
    instance = V2Server(
        engine, port=0, db_path=tmp_path / "limits.db",
        auth_required=True, limits=limits, clock=clock,
        # Keep the production poll out of the way: the test advances the
        # injected clock and calls the sweep explicitly.
        idle_poll_seconds=3600.0)
    task = asyncio.create_task(instance.run())
    await _wait_for_bind(instance)
    try:
        yield instance, engine
    finally:
        await _stop(instance, task)


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


def test_limits_default_to_ten_per_second_burst_twenty_and_fifteen_minutes(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.limits.commands_per_second == 10.0
    assert config.limits.command_burst == 20
    assert config.limits.idle_timeout_seconds == 900.0


def test_limits_are_read_from_mud_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mud.toml").write_text(
        "[limits]\n"
        "commands_per_second = 2.5\n"
        "command_burst = 3\n"
        "idle_timeout_seconds = 90\n",
        encoding="utf-8",
    )

    config = load_config()

    assert config.limits.commands_per_second == 2.5
    assert config.limits.command_burst == 3
    assert config.limits.idle_timeout_seconds == 90.0


@pytest.mark.parametrize("body", [
    "[limits]\ncommands_per_second = 0\n",
    "[limits]\ncommands_per_second = -1\n",
    "[limits]\ncommands_per_second = true\n",
    '[limits]\ncommands_per_second = "10"\n',
    "[limits]\ncommands_per_second = inf\n",
    "[limits]\ncommand_burst = 0\n",
    "[limits]\ncommand_burst = -1\n",
    "[limits]\ncommand_burst = true\n",
    '[limits]\ncommand_burst = "20"\n',
    "[limits]\ncommand_burst = 2.5\n",
    "[limits]\nidle_timeout_seconds = 0\n",
    "[limits]\nidle_timeout_seconds = -1\n",
    "[limits]\nidle_timeout_seconds = true\n",
    '[limits]\nidle_timeout_seconds = "900"\n',
    "[limits]\nidle_timeout_seconds = inf\n",
])
def test_limits_reject_non_positive_or_mistyped_values(tmp_path, body):
    path = tmp_path / "bad.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ConfigError, match="limits"):
        load_config(path)


def test_limits_must_be_a_table(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('limits = "not a table"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="limits"):
        load_config(path)


# ----------------------------------------------------------------------
# Command rate limit
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_command_over_the_burst_is_rate_limited_without_a_disconnect(
        tmp_path):
    """The 3rd command in a burst of 2 is direct, typed, and recoverable."""
    clock = Clock()
    limits = LimitsConfig(
        commands_per_second=1.0, command_burst=2,
        idle_timeout_seconds=900.0)
    async with limits_server(
            tmp_path, limits=limits, clock=clock) as (instance, _engine), \
            Client(f"ws://localhost:{instance.port}") as ada:
        await ada.register("Ada", "c-1")

        for msg_id in ("ok-1", "ok-2"):
            await ada.input("look", msg_id)
            state = await ada.next("room.state", msg_id=msg_id)
            assert state["data"]["room"]["id"]

        await ada.input("look", "limited")
        limited = await ada.next("error", msg_id="limited")
        assert limited["id"] == "limited"
        assert limited["data"]["code"] == "rate_limited"
        assert limited["data"]["text"]
        # The limit is not a close, and the request never reached the
        # engine: no room.state follows it.
        assert ada.ws.close_code is None

        # A full second refills exactly one token at 1 command/s.
        clock.advance(1.0)
        await ada.input("look", "recovered")
        state = await ada.next("room.state", msg_id="recovered")
        assert state["data"]["room"]["id"]
        assert ada.ws.close_code is None


@pytest.mark.asyncio
async def test_the_command_budget_is_per_connection(tmp_path):
    clock = Clock()
    limits = LimitsConfig(
        commands_per_second=1.0, command_burst=1,
        idle_timeout_seconds=900.0)
    async with limits_server(tmp_path, limits=limits, clock=clock) as (
            instance, _engine):
        uri = f"ws://localhost:{instance.port}"
        async with Client(uri) as ada:
            await ada.register("Ada", "c-1")
            await ada.input("look", "ada-ok")
            await ada.next("room.state", msg_id="ada-ok")
            await ada.input("look", "ada-limited")
            limited = await ada.next("error", msg_id="ada-limited")
            assert limited["data"]["code"] == "rate_limited"

            # Another connection has its own bucket; Ada's throttle is not a
            # name lock and not a server-wide limit.
            async with Client(uri) as ben:
                await ben.register("Ben", "c-2")
                await ben.input("look", "ben-ok")
                state = await ben.next("room.state", msg_id="ben-ok")
                assert state["data"]["room"]["id"]
                assert ben.ws.close_code is None


@pytest.mark.asyncio
async def test_quit_is_never_rate_limited(tmp_path):
    """A client that spent its budget can still leave cleanly."""
    clock = Clock()
    limits = LimitsConfig(
        commands_per_second=1.0, command_burst=1,
        idle_timeout_seconds=900.0)
    async with limits_server(
            tmp_path, limits=limits, clock=clock) as (instance, _engine), \
            Client(f"ws://localhost:{instance.port}") as ada:
        await ada.register("Ada", "c-1")
        await ada.input("look", "spent")
        await ada.next("room.state", msg_id="spent")
        await ada.input("look", "limited")
        limited = await ada.next("error", msg_id="limited")
        assert limited["data"]["code"] == "rate_limited"

        await ada.input("quit", "bye")
        assert await ada.close_code() == 1000


# ----------------------------------------------------------------------
# Idle timeout
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_client_sleeps_in_place_and_closes_with_4004(tmp_path):
    clock = Clock()
    limits = LimitsConfig(
        commands_per_second=10.0, command_burst=20,
        idle_timeout_seconds=30.0)
    async with limits_server(tmp_path, limits=limits, clock=clock) as (
            instance, engine):
        uri = f"ws://localhost:{instance.port}"
        ada = await Client(uri).connect()
        key = await ada.register("Ada", "c-1")
        ben = await Client(uri).connect()
        await ben.register("Ben", "c-2")
        await ada.next("room.enter")  # Ben arrived where Ada is

        room = engine.get_player("Ada").location
        assert room is not None

        clock.advance(31.0)
        # Ben proves he is awake at the same instant; only Ada's last message
        # is old enough for the sweep.
        await ben.input("look", "ben-awake")
        await ben.next("room.state", msg_id="ben-awake")
        await instance.check_idle_timeouts()

        assert await ada.close_code() == IDLE_TIMEOUT_CLOSE

        # #72 sleep, not a guest removal: the character is still in the room,
        # asleep and stone, and the dropped-on-sleep items are on the floor.
        sleeping = engine.get_player("Ada")
        assert sleeping is not None
        assert sleeping.asleep is True
        assert sleeping.location is room

        left = await ben.next("room.leave")
        assert left["data"]["actor"]["name"] == "Ada"
        assert left["data"]["reason"] == "disconnect"
        assert left["data"]["asleep"] is True
        assert left["data"]["stone"] is True

        # `who` shows the sleeping character, awake or not (#72).
        who = await ben.who("who-idle")
        players = {entry["name"]: entry for entry in who["data"]["players"]}
        assert players["Ada"]["asleep"] is True
        assert players["Ada"]["stone"] is True
        assert players["Ben"]["asleep"] is False

        # Login wakes the same character in the same room; sleep was not a
        # disconnect-removal.
        async with Client(uri) as again:
            ok, state = await again.login(key, "c-3")
            assert ok["data"]["player"] == {"name": "Ada"}
            assert state["data"]["room"]["id"] == room_key(room)
            assert engine.get_player("Ada").asleep is False

        await ben.close()


@pytest.mark.asyncio
async def test_client_activity_resets_the_idle_clock(tmp_path):
    clock = Clock()
    limits = LimitsConfig(
        commands_per_second=10.0, command_burst=20,
        idle_timeout_seconds=30.0)
    async with limits_server(
            tmp_path, limits=limits, clock=clock) as (instance, _engine), \
            Client(f"ws://localhost:{instance.port}") as ada:
        await ada.register("Ada", "c-1")

        clock.advance(20.0)
        await ada.input("look", "still-awake")
        await ada.next("room.state", msg_id="still-awake")

        # 20s since the last message, not 40s since registration.
        clock.advance(20.0)
        await instance.check_idle_timeouts()
        assert ada.ws.close_code is None

        # Ten more seconds empty the 30s budget since that last message.
        clock.advance(10.0)
        await instance.check_idle_timeouts()
        assert await ada.close_code() == IDLE_TIMEOUT_CLOSE
