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

from contextlib import asynccontextmanager

import pytest

from src.mud.config import ConfigError, LimitsConfig, load_config
from src.mud.engine.core import Engine
from src.mud.world.loader import load_demo_village
from tests.golden.harness import HarnessClient as Client
from tests.golden.harness import server_harness

IDLE_TIMEOUT_CLOSE = 4004


class Clock:
    """A manual monotonic clock: the test advances time, never sleeps."""

    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@asynccontextmanager
async def limits_server(tmp_path, *, limits: LimitsConfig, clock: Clock):
    engine = Engine()
    await load_demo_village(engine)
    async with server_harness(
        engine,
        db_path=tmp_path / "limits.db",
        auth_required=True,
        limits=limits,
        clock=clock,
        # Keep the production poll out of the way: the test advances the
        # injected clock and calls the sweep explicitly.
        idle_poll_seconds=3600.0,
    ) as server:
        yield server


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
            tmp_path, limits=limits, clock=clock) as server, \
            Client(server) as ada:
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
    async with limits_server(tmp_path, limits=limits, clock=clock) as server, \
            Client(server) as ada:
        await ada.register("Ada", "c-1")
        await ada.input("look", "ada-ok")
        await ada.next("room.state", msg_id="ada-ok")
        await ada.input("look", "ada-limited")
        limited = await ada.next("error", msg_id="ada-limited")
        assert limited["data"]["code"] == "rate_limited"

        # Another connection has its own bucket; Ada's throttle is not a
        # name lock and not a server-wide limit.
        async with Client(server) as ben:
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
            tmp_path, limits=limits, clock=clock) as server, \
            Client(server) as ada:
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
    async with limits_server(tmp_path, limits=limits, clock=clock) as server:
        ada = await server.connect()
        key = await ada.register_key("Ada", "c-1")
        ben = await server.connect()
        await ben.register("Ben", "c-2")
        await ada.next("room.enter")  # Ben arrived where Ada is

        clock.advance(31.0)
        # Ben proves he is awake at the same instant; only Ada's last message
        # is old enough for the sweep.
        await ben.input("look", "ben-awake")
        await ben.next("room.state", msg_id="ben-awake")
        await server.instance.sessions.expire_idle()

        assert await ada.close_code() == IDLE_TIMEOUT_CLOSE

        # #72 sleep, not a guest removal: the character is still in the room,
        # asleep and stone. Both facts are observable over the protocol.
        left = await ben.next("room.leave")
        assert left["data"]["actor"]["name"] == "Ada"
        assert left["data"]["reason"] == "disconnect"
        assert left["data"]["asleep"] is True
        assert left["data"]["stone"] is True

        state = await ben.state("look-idle")
        assert {"name": "Ada", "asleep": True, "stone": True} in (
            state["room"]["players"])
        assert state["room"]["id"] == "gate"

        # `who` shows the sleeping character, awake or not (#72).
        players = {
            entry["name"]: entry
            for entry in (await server.who("Ben"))["players"]
        }
        assert players["Ada"]["asleep"] is True
        assert players["Ada"]["stone"] is True
        assert players["Ben"]["asleep"] is False

        # Login wakes the same character in the same room; sleep was not a
        # disconnect-removal. Query while the woken connection is still live:
        # closing it sleeps Ada again.
        async with server.client() as again:
            ok, state = await again.login(key, "c-3")
            assert ok["data"]["player"] == {"name": "Ada"}
            assert state["data"]["room"]["id"] == "gate"

            players = {
                entry["name"]: entry
                for entry in (await server.who("Ben"))["players"]
            }
            assert players["Ada"]["asleep"] is False
            assert players["Ada"]["stone"] is False

        await ben.close()


@pytest.mark.asyncio
async def test_client_activity_resets_the_idle_clock(tmp_path):
    clock = Clock()
    limits = LimitsConfig(
        commands_per_second=10.0, command_burst=20,
        idle_timeout_seconds=30.0)
    async with limits_server(
            tmp_path, limits=limits, clock=clock) as server, \
            Client(server) as ada:
        await ada.register("Ada", "c-1")

        clock.advance(20.0)
        await ada.input("look", "still-awake")
        await ada.next("room.state", msg_id="still-awake")

        # 20s since the last message, not 40s since registration.
        clock.advance(20.0)
        await server.instance.sessions.expire_idle()
        assert ada.ws.close_code is None

        # Ten more seconds empty the 30s budget since that last message.
        clock.advance(10.0)
        await server.instance.sessions.expire_idle()
        assert await ada.close_code() == IDLE_TIMEOUT_CLOSE
