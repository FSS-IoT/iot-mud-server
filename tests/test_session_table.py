"""Ticket #134: the session table owns presence and delivery.

These tests pin the interface the server uses after the split:

* `Session` owns its socket, identity phase, per-connection limiters, last
  activity, and idle deadline.
* `SessionTable` owns both socket maps, room and private delivery, forget, and
  idle expiry.
* `V2Server` owns neither map; tests query the table instead of private state.

Every server here binds port 0 against a temporary database. Nothing reads a
private `_sessions`/`_ws_by_name` map.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from src.mud.config import AuthConfig, LimitsConfig
from src.mud.content import Room
from src.mud.engine.core import Engine
from src.mud.net.v2_server import (
    IDLE_TIMEOUT_CLOSE,
    Session,
    SessionTable,
    V2Server,
)
from src.mud.world.loader import load_demo_village


class Clock:
    """A manual monotonic clock: the test advances time, never sleeps."""

    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeSocket:
    """The two socket methods a session uses, with no network."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.close_code: int | None = None

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


def _world() -> tuple[Engine, Room]:
    """One room and a fresh engine; players spawn in the room."""
    room = Room(name="Hall", description="A test hall.")
    room.item_id = "hall"
    engine = Engine()
    engine.rooms[room.name] = room
    engine.start_room = room
    return engine, room


def _bound(
    table: SessionTable,
    clock: Clock,
    name: str,
    *,
    authenticated: bool = True,
    idle_timeout: float = 30.0,
) -> tuple[Session, FakeSocket]:
    ws = FakeSocket()
    session = Session(
        ws=ws,
        clock=clock,
        limits=LimitsConfig(idle_timeout_seconds=idle_timeout),
    )
    session.bind(name, authenticated=authenticated)
    table.bind(session)
    return session, ws


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_session_table_reports_quiescent_after_disconnect(tmp_path):
    """The guest handler's disconnect leaves the table empty and queryable."""
    engine = Engine()
    await load_demo_village(engine)
    server = V2Server(
        engine,
        port=0,
        db_path=tmp_path / "sessions.db",
        auth_required=False,
        idle_poll_seconds=3600.0,
    )
    task = asyncio.create_task(server.run())
    try:
        for _ in range(500):
            if server.port:
                break
            await asyncio.sleep(0.01)
        assert server.port, "v2 server did not bind"

        ws = await websockets.connect(f"ws://localhost:{server.port}")
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello"
        await ws.send(json.dumps({
            "v": 2, "type": "join", "id": "join",
            "data": {"name": "Ada"},
        }))
        assert json.loads(await ws.recv())["type"] == "session.ok"
        assert json.loads(await ws.recv())["type"] == "room.state"

        assert server.sessions.active == 1
        assert server.sessions.session_for_name("Ada") is not None
        assert server.sessions.socket_for_name("Ada") is not None

        await ws.close()
        await _wait_for(lambda: server.sessions.is_quiescent)

        assert server.sessions.active == 0
        assert server.sessions.session_for_name("Ada") is None
        assert server.sessions.socket_for_name("Ada") is None
    finally:
        server.stop()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except TimeoutError:
            task.cancel()


@pytest.mark.asyncio
async def test_idle_expiry_is_owned_by_the_session():
    """The table expires one idle session; the server never scans a map."""
    clock = Clock()
    engine, room = _world()
    engine.register_player("Ada")
    engine.register_player("Ben")
    table = SessionTable(engine)

    ada, ada_ws = _bound(table, clock, "Ada")
    ben, ben_ws = _bound(table, clock, "Ben")

    clock.advance(30.0)
    ben.touch()  # Ben is awake at the same instant; only Ada is expired.

    expired = await table.expire_idle()

    assert [session.player_name for session in expired] == ["Ada"]
    assert ada_ws.close_code == IDLE_TIMEOUT_CLOSE
    assert ada.slept is True
    sleeping = engine.get_player("Ada")
    assert sleeping is not None
    assert sleeping.asleep is True
    assert sleeping.location is room
    assert table.session_for_name("Ada") is None
    assert table.socket_for_name("Ada") is None
    assert table.session_for_name("Ben") is ben
    assert table.active == 1
    assert table.is_quiescent is False

    # The shared sleep path told the room before the idle close.
    assert [message["type"] for message in ben_ws.sent] == ["room.leave"]
    assert ben_ws.sent[0]["data"]["asleep"] is True
    assert ben_ws.sent[0]["data"]["stone"] is True
    assert ben_ws.sent[0]["data"]["actor"]["name"] == "Ada"


def test_name_to_socket_mapping_is_one_invariant():
    """bind and forget keep both maps in step; a rebind is not a duplicate."""
    clock = Clock()
    engine, _room = _world()
    table = SessionTable(engine)
    ws = FakeSocket()
    session = Session(ws=ws, clock=clock)

    table.bind(session)
    assert table.active == 1
    assert session.phase == "anonymous"
    assert table.socket_for_name("Ada") is None

    session.bind("Ada", authenticated=True)
    assert session.phase == "authenticated"
    table.bind(session)
    assert table.socket_for_name("Ada") is ws
    assert table.session_for_name("Ada") is session

    # Re-binding the same session is the same one session, not a second entry.
    table.bind(session)
    assert table.active == 1
    assert table.socket_for_name("Ada") is ws

    table.forget(session)
    assert table.is_quiescent is True
    assert table.active == 0
    assert table.session_for_name("Ada") is None
    assert table.socket_for_name("Ada") is None


def test_v2_server_no_longer_owns_the_session_maps():
    """Presence is the table's: the server exposes no private map attribute."""
    server = V2Server(Engine(), port=0)

    assert isinstance(server.sessions, SessionTable)
    assert not hasattr(server, "_sessions")
    assert not hasattr(server, "_ws_by_name")


@pytest.mark.asyncio
async def test_private_delivery_reaches_only_the_named_session():
    """One private push goes to one character; unknown names are dropped."""
    clock = Clock()
    engine, _room = _world()
    engine.register_player("Ada")
    engine.register_player("Ben")
    table = SessionTable(engine)

    _ada, ada_ws = _bound(table, clock, "Ada")
    _ben, ben_ws = _bound(table, clock, "Ben")

    delivered = await table.send_to_player(
        "Ben", "player.told",
        {"from": "Ada", "to": "Ben", "text_actor": 'Ada tells you, "hi".',
         "self": False},
    )

    assert delivered is True
    assert [message["type"] for message in ben_ws.sent] == ["player.told"]
    assert ben_ws.sent[0]["data"]["text_actor"] == 'Ada tells you, "hi".'
    assert ada_ws.sent == []

    assert await table.send_to_player("Nobody", "player.told", {}) is False


def test_session_owns_its_auth_budget_and_command_bucket():
    """Both per-connection limiters live on the session, not the server."""
    clock = Clock()
    session = Session(
        ws=FakeSocket(),
        clock=clock,
        auth_limits=AuthConfig(max_attempts=2, window_seconds=10.0),
        limits=LimitsConfig(
            commands_per_second=1.0, command_burst=2,
            idle_timeout_seconds=30.0),
    )

    session.note_auth_failure()
    session.note_auth_failure()
    assert session.auth_is_limited() is True
    clock.advance(10.0)
    assert session.auth_is_limited() is False

    assert session.take_command() is True
    assert session.take_command() is True
    assert session.take_command() is False
    clock.advance(1.0)
    assert session.take_command() is True

    clock.advance(30.0)
    assert session.idle_expired() is True
