"""Real-server harness for the golden scenarios (#83).

A scenario gets a fresh `V2Server` bound to `port=0` with a temporary SQLite
database seeded from the product world (`data/worlds/club_world`). Nothing is
shared between scenarios: no fixed port, no database file, no wall-clock tick.
The background NPC tick is parked (`tick_seconds` is longer than any test), so
a scenario drives `wander_tick()` itself and the run is reproducible.

Two modes are offered, both real servers:

* auth-required (the v2.1 default) for the product paths;
* the explicit v2.0 guest opt-out, needed to deliver `session.ok` and the
  `not_joined` error the protocol still documents.

`GoldenClient` records every frame it receives in `.log`; coverage reads those
logs rather than a hand-maintained checklist, so a scenario that stops
delivering an event fails the coverage test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import websockets

from src.mud.config import FeaturesConfig, LimitsConfig
from src.mud.content.models import load_world as load_content_world
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.storage.db import open_database
from src.mud.world.loader import load_world
from src.mud.world.seed import seed_world

REPO_ROOT = Path(__file__).resolve().parents[2]
WORLD_DIR = REPO_ROOT / "data" / "worlds" / "club_world"

# The product world's room -> area map. It is read from the same content files
# the loader uses, so a new room or area shows up as missing coverage instead
# of silently passing.
ROOM_AREAS: dict[str, str] = {
    room.id: room.area
    for room in load_content_world(WORLD_DIR).world.rooms
}
ALL_ROOMS = frozenset(ROOM_AREAS)
ALL_AREAS = frozenset(ROOM_AREAS.values())

# A test client sends a whole tour in a few hundred milliseconds. The defaults
# (#85: 10/s, burst 20) are production limits and would make a golden run
# rate-limited, so the harness injects high ones. The limit behavior itself is
# tested in tests/test_limits.py, not here.
GOLDEN_LIMITS = LimitsConfig(
    commands_per_second=1000.0,
    command_burst=1000,
    idle_timeout_seconds=900.0,
)

# Park the 1 Hz NPC task. Scenarios call `wander_tick()` when they mean to
# advance the world, so two runs of the same scenario see the same events.
PARKED_TICK_SECONDS = 3600.0


@dataclass
class GoldenRun:
    """What one scenario observed, for the coverage assertion."""

    name: str
    rooms: set[str] = field(default_factory=set)
    events: set[str] = field(default_factory=set)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def areas(self) -> set[str]:
        return {ROOM_AREAS[room] for room in self.rooms}

    def note_room(self, room_id: str) -> None:
        self.rooms.add(room_id)

    def collect(self, *clients: GoldenClient) -> None:
        for client in clients:
            self.events.update(message["type"] for message in client.log)


@dataclass
class GoldenServer:
    instance: V2Server
    engine: Engine
    port: int
    db_path: Path

    @property
    def uri(self) -> str:
        return f"ws://localhost:{self.port}"


class GoldenClient:
    """A v2 client that records everything it receives and waits by type."""

    def __init__(self, uri: str):
        self.uri = uri
        self.ws: Any = None
        self.log: list[dict] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        # Frames that arrived while a different type was awaited. `wait` keeps
        # them instead of dropping them, so a scenario can assert a private
        # event later without depending on the order it called `wait`.
        self.pending: list[dict] = []
        self.task: asyncio.Task | None = None
        self.hello: dict | None = None
        self._next_id = 0
        self._closed = False

    async def connect(self) -> GoldenClient:
        self.ws = await websockets.connect(self.uri)
        self.hello = json.loads(await self.ws.recv())
        self.log.append(self.hello)
        self.task = asyncio.create_task(self._reader())
        return self

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                self.log.append(message)
                self.queue.put_nowait(message)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send(self, message: dict) -> None:
        await self.ws.send(json.dumps(message))

    async def input(self, text: str) -> int:
        self._next_id += 1
        await self.send({
            "v": 2, "type": "input", "id": self._next_id,
            "data": {"text": text},
        })
        return self._next_id

    async def register(self, name: str) -> tuple[dict, dict]:
        await self.send({
            "v": 2, "type": "auth", "id": "register",
            "data": {"mode": "register", "name": name},
        })
        ok = await self.wait("auth.ok")
        state = await self.wait("room.state")
        return ok, state

    async def login(self, key: str) -> tuple[dict, dict]:
        await self.send({
            "v": 2, "type": "auth", "id": "login",
            "data": {"mode": "login", "key": key},
        })
        ok = await self.wait("auth.ok")
        state = await self.wait("room.state")
        return ok, state

    async def join(self, name: str) -> tuple[dict, dict]:
        await self.send({
            "v": 2, "type": "join", "id": "join",
            "data": {"name": name},
        })
        ok = await self.wait("session.ok")
        state = await self.wait("room.state")
        return ok, state

    async def auth_failure(self, data: dict, msg_id: str) -> dict:
        await self.send({
            "v": 2, "type": "auth", "id": msg_id, "data": data,
        })
        return await self.wait("auth.error")

    async def wait(
        self,
        type_: str,
        predicate: Callable[[dict], bool] | None = None,
        timeout: float = 5.0,
    ) -> dict:
        for index, message in enumerate(self.pending):
            if message.get("type") == type_ and (
                predicate is None or predicate(message)
            ):
                return self.pending.pop(index)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                seen = sorted({message["type"] for message in self.log})
                raise TimeoutError(
                    f"no {type_!r} within {timeout}s; saw {seen}"
                )
            message = await asyncio.wait_for(self.queue.get(), remaining)
            if message.get("type") == type_ and (
                predicate is None or predicate(message)
            ):
                return message
            self.pending.append(message)

    async def quit(self) -> None:
        await self.send({"v": 2, "type": "quit"})

    def types(self) -> set[str]:
        return {message["type"] for message in self.log}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.task is not None:
            self.task.cancel()
            # `return_exceptions=True` absorbs the cancellation and any reader
            # failure; teardown has nothing useful to do with either.
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.ws is not None:
            await self.ws.close()
            self.ws = None


@asynccontextmanager
async def golden_server(
    directory: str | Path,
    *,
    auth_required: bool = True,
) -> AsyncIterator[GoldenServer]:
    """Start a fresh seeded server on port 0 and stop it afterwards."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / "golden.db"

    # Seed the durable world before the server opens its own connection. The
    # engine still loads the same YAML separately; only the server's `hello`,
    # auth, and write-through paths need the database row.
    conn = open_database(db_path)
    try:
        seed_world(conn, WORLD_DIR, epoch="golden-epoch")
    finally:
        conn.close()

    engine = Engine()
    await load_world(engine, WORLD_DIR)
    instance = V2Server(
        engine,
        port=0,
        db_path=db_path,
        auth_required=auth_required,
        limits=GOLDEN_LIMITS,
        # #84: effects stay on the bus and in event_log. Golden coverage is
        # about client-delivered events, so the flag is pinned off even if a
        # later change makes it default on.
        features=FeaturesConfig(effects=False),
        tick_seconds=PARKED_TICK_SECONDS,
    )
    task = asyncio.create_task(instance.run())
    for _ in range(500):
        if instance.port:
            break
        await asyncio.sleep(0.01)
    assert instance.port, "golden server did not bind"
    try:
        yield GoldenServer(
            instance=instance, engine=engine, port=instance.port,
            db_path=db_path,
        )
    finally:
        instance.stop()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def golden_client(server: GoldenServer) -> AsyncIterator[GoldenClient]:
    client = GoldenClient(server.uri)
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


async def wait_until(
    predicate: Callable[[], bool],
    timeout: float = 2.0,
    description: str = "condition",
) -> None:
    """Poll the live engine for a state the server reaches asynchronously.

    Closing a socket is not instantaneous: the server's cleanup (which sleeps
    the character) runs in its own task. Scenarios that immediately log a
    sleeping character back in wait here instead of guessing a sleep.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"timed out waiting for {description}")
        await asyncio.sleep(0.005)


def room_by_id(engine: Engine, room_id: str):
    for room in engine.rooms.values():
        if room.item_id == room_id:
            return room
    raise KeyError(f"no loaded room {room_id!r}")


def direction_between(engine: Engine, source_id: str, target_id: str) -> str:
    """The exit direction that walks from one loaded room to another."""
    source = room_by_id(engine, source_id)
    for direction, target in source.exits.items():
        if getattr(target, "item_id", None) == target_id:
            return direction
    raise AssertionError(f"{source_id} has no exit to {target_id}")


async def command(
    client: GoldenClient,
    text: str,
    expect: str,
    predicate: Callable[[dict], bool] | None = None,
) -> dict:
    await client.input(text)
    return await client.wait(expect, predicate)


async def go(
    run: GoldenRun,
    client: GoldenClient,
    direction: str,
    expected_room_id: str,
) -> dict:
    state = await command(client, f"go {direction}", "room.state")
    assert state["data"]["room"]["id"] == expected_room_id, (
        f"go {direction} landed in {state['data']['room']['id']!r}, "
        f"expected {expected_room_id!r}"
    )
    run.note_room(expected_room_id)
    return state


async def walk(
    run: GoldenRun,
    client: GoldenClient,
    engine: Engine,
    *room_ids: str,
) -> None:
    """Walk a route of room ids, resolving each hop to a `go` direction."""
    for source, target in pairwise(room_ids):
        await go(
            run, client, direction_between(engine, source, target), target
        )
