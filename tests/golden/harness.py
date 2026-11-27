"""Real-server harness for every v2 test (#83, #136).

One server lifecycle and one client are shared by the golden scenarios, the
unit-level protocol tests, and the load smoke:

* `ServerHarness` starts a real `V2Server` with `await server.start()` (port 0
  by default) and stops it with `await instance.close()`. It never polls the
  port, so a caller can read `endpoint`/`uri` as soon as `start()` returns.
* `HarnessClient` records every frame in `.log` and waits by type or request
  id. The harness answers the observable questions `state(actor)`, `who()`, and
  `events()` from live protocol traffic instead of `_sessions` or engine
  internals.
* `server_harness(engine, db_path=...)` wraps any engine and database;
  `golden_server()` seeds the product world and pins the golden limits.

The golden scenarios still get a fresh database seeded from
`data/worlds/club_world` and a parked NPC tick, so nothing is shared between
scenarios: no fixed port, no database file, no wall-clock tick. The auth
default is the v2.1 product path; `auth_required=False` is the explicit v2.0
guest opt-out used by the tests that still document it.

`GoldenClient`/`GoldenServer` remain aliases so older call sites keep one
implementation rather than a second copy.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, Self

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

    def collect(self, *clients: HarnessClient) -> None:
        for client in clients:
            self.events.update(message["type"] for message in client.log)


@dataclass
class ServerHarness:
    """A started v2 server plus the clients the harness can query.

    `port` is the endpoint `start()` returned, never a value discovered by
    polling. `state(actor)` sends `look` on that actor's live client, `who()`
    sends `who`, and `events()` reads the frames the client really received.
    """

    instance: V2Server
    engine: Engine
    port: int
    db_path: Path | None = None
    _clients: dict[str, HarnessClient] = field(
        default_factory=dict, repr=False)

    @property
    def uri(self) -> str:
        return f"ws://{self.instance.host}:{self.port}"

    async def connect(self) -> HarnessClient:
        """Open one client and return it connected and reading frames."""
        return await HarnessClient(self).connect()

    @asynccontextmanager
    async def client(self) -> AsyncIterator[HarnessClient]:
        """Open one client for an `async with` block."""
        client = await self.connect()
        try:
            yield client
        finally:
            await client.close()

    def _bind(self, client: HarnessClient) -> None:
        if client.name is not None:
            self._clients[client.name] = client

    def _unbind(self, client: HarnessClient) -> None:
        name = client.name
        if name is not None and self._clients.get(name) is client:
            self._clients.pop(name, None)

    def _live(self, actor: str | None = None) -> HarnessClient:
        if actor is None:
            if not self._clients:
                raise LookupError("no live harness client")
            return next(iter(self._clients.values()))
        client = self._clients.get(actor)
        if client is None:
            raise LookupError(f"no live harness client for {actor!r}")
        return client

    async def state(self, actor: str) -> dict:
        """The actor's live `room.state` payload, requested with `look`."""
        client = self._live(actor)
        msg_id = await client.input("look")
        event = await client.wait(
            "room.state", lambda message: message.get("id") == msg_id)
        return event["data"]

    async def who(self, actor: str | None = None) -> dict:
        """The server-wide roster payload from one live client."""
        return (await self._live(actor).who())["data"]

    def events(self, actor: str | None = None,
               type_: str | None = None) -> list[dict]:
        """Frames received by `actor`, or by every live client in order."""
        if actor is None:
            messages = [
                message
                for client in self._clients.values()
                for message in client.log
            ]
        else:
            messages = list(self._live(actor).log)
        if type_ is None:
            return messages
        return [
            message for message in messages
            if message.get("type") == type_
        ]


class HarnessClient:
    """One v2 client: background reader, frame log, and typed waiting.

    It is the only client implementation in the test tree. The golden
    scenarios use `.log`; unit tests use `register`/`login`/`join`, `input`,
    `next`, `who`, `inventory`, and `close_code`. Passing a `ServerHarness`
    instead of a URI also binds the character name for `state`/`who`/`events`.
    """

    def __init__(self, target: str | ServerHarness):
        if isinstance(target, ServerHarness):
            self.harness: ServerHarness | None = target
            self.uri = target.uri
        else:
            self.harness = None
            self.uri = target
        self.ws: Any = None
        self.log: list[dict] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        # Frames that arrived while a different type was awaited. `wait` keeps
        # them instead of dropping them, so a scenario can assert a private
        # event later without depending on the order it called `wait`.
        self.pending: list[dict] = []
        self.task: asyncio.Task | None = None
        self.hello: dict | None = None
        self.name: str | None = None
        self._next_id = 0
        self._closed = False

    async def connect(self) -> HarnessClient:
        self.ws = await websockets.connect(self.uri)
        self.hello = json.loads(await self.ws.recv())
        self.log.append(self.hello)
        self.task = asyncio.create_task(self._reader())
        return self

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                self.log.append(message)
                self.queue.put_nowait(message)
        except websockets.exceptions.ConnectionClosed:
            pass

    def _identify(self, name: str) -> None:
        self.name = name
        if self.harness is not None:
            self.harness._bind(self)

    async def send(self, message: dict) -> None:
        await self.ws.send(json.dumps(message))

    async def input(self, text: str, msg_id: Any = None) -> Any:
        if msg_id is None:
            self._next_id += 1
            msg_id = self._next_id
        else:
            try:
                self._next_id = max(self._next_id, int(msg_id))
            except (TypeError, ValueError):
                pass
        await self.send({
            "v": 2, "type": "input", "id": msg_id,
            "data": {"text": text},
        })
        return msg_id

    async def register(
        self, name: str, msg_id: Any = "register",
    ) -> tuple[dict, dict]:
        await self.send({
            "v": 2, "type": "auth", "id": msg_id,
            "data": {"mode": "register", "name": name},
        })
        ok = await self.wait(
            "auth.ok", lambda message: message.get("id") == msg_id)
        state = await self.wait("room.state")
        self._identify(ok["data"]["player"]["name"])
        return ok, state

    async def register_key(
        self, name: str, msg_id: Any = "register",
    ) -> str:
        """Register and return the one-time character key."""
        ok, _state = await self.register(name, msg_id)
        return ok["data"]["key"]

    async def login(self, key: str, msg_id: Any = "login") -> tuple[dict, dict]:
        await self.send({
            "v": 2, "type": "auth", "id": msg_id,
            "data": {"mode": "login", "key": key},
        })
        ok = await self.wait(
            "auth.ok", lambda message: message.get("id") == msg_id)
        state = await self.wait("room.state")
        self._identify(ok["data"]["player"]["name"])
        return ok, state

    async def join(self, name: str, msg_id: Any = "join") -> tuple[dict, dict]:
        await self.send({
            "v": 2, "type": "join", "id": msg_id,
            "data": {"name": name},
        })
        ok = await self.wait(
            "session.ok", lambda message: message.get("id") == msg_id)
        state = await self.wait("room.state")
        self._identify(ok["data"]["player"]["name"])
        return ok, state

    async def auth_failure(self, data: dict, msg_id: str) -> dict:
        await self.auth(data, msg_id)
        return await self.wait("auth.error")

    async def auth(self, data: dict, msg_id: Any = "auth") -> None:
        await self.send({
            "v": 2, "type": "auth", "id": msg_id, "data": data,
        })

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

    async def next(
        self, type_: str, *, msg_id: Any = None, timeout: float = 5.0,
    ) -> dict:
        """Typed wait; with `msg_id`, require the direct reply's id."""
        return await self.wait(
            type_,
            lambda message: (
                msg_id is None or message.get("id") == msg_id),
            timeout,
        )

    async def next_any(self, timeout: float = 5.0) -> dict:
        if self.pending:
            return self.pending.pop(0)
        return await asyncio.wait_for(self.queue.get(), timeout)

    async def drain(self, timeout: float = 0.2) -> list[dict]:
        """Read everything queued, stopping after `timeout` of silence."""
        seen: list[dict] = []
        while True:
            try:
                seen.append(await self.next_any(timeout))
            except TimeoutError:
                return seen

    async def assert_silent(self, timeout: float = 0.25) -> None:
        """Fail when any frame arrives within `timeout`."""
        try:
            message = await self.next_any(timeout)
        except TimeoutError:
            return
        raise AssertionError(
            f"unexpected {message.get('type')!r} frame: {message}")

    async def absent(self, type_: str, timeout: float = 0.3) -> bool:
        """True when no `type_` message is already waiting or arrives soon."""
        if any(message.get("type") == type_ for message in self.pending):
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return True
            try:
                message = await asyncio.wait_for(
                    self.queue.get(), remaining)
            except TimeoutError:
                return True
            if message.get("type") == type_:
                return False
            self.pending.append(message)

    async def state(self, msg_id: Any = None) -> dict:
        """Send `look`; return the live `room.state` payload."""
        msg_id = await self.input("look", msg_id)
        event = await self.wait(
            "room.state", lambda message: message.get("id") == msg_id)
        return event["data"]

    async def who(self, msg_id: Any = "who") -> dict:
        msg_id = await self.input("who", msg_id)
        return await self.wait(
            "who.list", lambda message: message.get("id") == msg_id)

    async def inventory(self, msg_id: Any = "inv") -> list[dict]:
        msg_id = await self.input("inventory", msg_id)
        event = await self.wait(
            "player.inventory", lambda message: message.get("id") == msg_id)
        return event["data"]["items"]

    async def quit(self) -> None:
        await self.send({"v": 2, "type": "quit"})

    async def close_code(self, timeout: float = 5.0) -> int | None:
        if self.ws is None:
            return None
        try:
            await asyncio.wait_for(self.ws.wait_closed(), timeout)
        except (TimeoutError, websockets.exceptions.ConnectionClosed):
            # A 4004 close is "abnormal" to the client API but is the
            # documented idle outcome; the code is still on the connection.
            pass
        return self.ws.close_code

    def types(self) -> set[str]:
        return {message["type"] for message in self.log}

    def events(self, type_: str | None = None) -> list[dict]:
        if type_ is None:
            return list(self.log)
        return [
            message for message in self.log
            if message.get("type") == type_
        ]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.harness is not None:
            self.harness._unbind(self)
        if self.task is not None:
            self.task.cancel()
            # `return_exceptions=True` absorbs the cancellation and any reader
            # failure; teardown has nothing useful to do with either.
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.ws is not None:
            await self.ws.close()
            self.ws = None


# The historical names stay aliases, not second implementations.
GoldenServer = ServerHarness
GoldenClient = HarnessClient


@asynccontextmanager
async def server_harness(
    engine: Engine,
    *,
    db_path: str | Path | None = None,
    host: str = "localhost",
    auth_required: bool = True,
    **options: Any,
) -> AsyncIterator[ServerHarness]:
    """Start any engine on port 0 and close it when the block exits."""
    instance = V2Server(
        engine,
        host=host,
        port=0,
        db_path=db_path,
        auth_required=auth_required,
        **options,
    )
    port = await instance.start()
    try:
        yield ServerHarness(
            instance=instance,
            engine=engine,
            port=port,
            db_path=Path(db_path) if db_path is not None else None,
        )
    finally:
        await instance.close()


@asynccontextmanager
async def golden_server(
    directory: str | Path,
    *,
    auth_required: bool = True,
) -> AsyncIterator[ServerHarness]:
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
    async with server_harness(
        engine,
        db_path=db_path,
        auth_required=auth_required,
        limits=GOLDEN_LIMITS,
        # #84: effects stay on the bus and in event_log. Golden coverage is
        # about client-delivered events, so the flag is pinned off even if a
        # later change makes it default on.
        features=FeaturesConfig(effects=False),
        tick_seconds=PARKED_TICK_SECONDS,
    ) as server:
        yield server


@asynccontextmanager
async def harness_client(
    server: ServerHarness | str,
) -> AsyncIterator[HarnessClient]:
    """A connected client for a harness or a bare URI."""
    client = await (
        server.connect()
        if isinstance(server, ServerHarness)
        else HarnessClient(server).connect()
    )
    try:
        yield client
    finally:
        await client.close()


# Golden call sites use this spelling; it is the same context manager.
golden_client = harness_client


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
    client: HarnessClient,
    text: str,
    expect: str,
    predicate: Callable[[dict], bool] | None = None,
) -> dict:
    await client.input(text)
    return await client.wait(expect, predicate)


async def go(
    run: GoldenRun,
    client: HarnessClient,
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
    client: HarnessClient,
    engine: Engine,
    *room_ids: str,
) -> None:
    """Walk a route of room ids, resolving each hop to a `go` direction."""
    for source, target in pairwise(room_ids):
        await go(
            run, client, direction_between(engine, source, target), target
        )
