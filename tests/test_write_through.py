"""Ticket #63: take/drop/move commit to SQLite before their events.

SPEC v2.1.0 criterion 2. The engine writes through the store first, applies the
change in memory second, and only then publishes the event. A failed write
raises before memory changes, so the adapter answers `error{internal}` and no
event reaches another client.

The hard-kill test drives a real v2 server on port 0 against a temporary
database, kills it with SIGKILL after the client has seen the mutations, and
reopens the file. Reloading a seeded world into the engine is #64; this test
proves the durable half.
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import websockets

from src.mud.content import Entity, Player, Room
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.protocol.adapter import execute
from src.mud.storage.db import open_database
from src.mud.storage.store import (
    ObjectLocation,
    PlayerStore,
    WorldStore,
    WriteThroughStore,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class World:
    engine: Engine
    hall: Room
    yard: Room
    coin: Entity
    lantern: Entity
    ada: Player


def _world(store: WriteThroughStore | None = None) -> World:
    """A two-room world: a coin on the floor, a lantern in Ada's hands."""
    engine = Engine(store=store)
    hall = Room("Hall", "A stone hall.")
    hall.item_id = "hall"
    yard = Room("Yard", "A muddy yard.")
    yard.item_id = "yard"
    hall.exits["north"] = yard
    yard.exits["south"] = hall

    coin = Entity("Coin", "A gold coin.", item_id="coin")
    coin.instance_id = "coin"
    lantern = Entity("Lantern", "A brass lantern.", item_id="lantern")
    lantern.instance_id = "lantern"
    hall.add_content(coin)

    engine._rooms[hall.name] = hall
    engine._rooms[yard.name] = yard
    engine.start_room = hall
    ada = engine.register_player("Ada")
    ada.add_content(lantern)
    return World(engine, hall, yard, coin, lantern, ada)


def _object_location(db_path: Path, key: str) -> ObjectLocation | None:
    """Read one object's location through a connection the server does not own."""
    conn = open_database(db_path)
    try:
        return WorldStore(conn).object_location(key)
    finally:
        conn.close()


def _player_room(db_path: Path, name: str) -> str | None:
    conn = open_database(db_path)
    try:
        return PlayerStore(conn, name).room_id
    finally:
        conn.close()


@pytest.fixture
def stored(tmp_path):
    """An engine writing through a store on a throwaway database."""
    conn = open_database(tmp_path / "mud.db")
    store = WriteThroughStore(conn)
    world = _world(store)
    try:
        yield world, store, tmp_path / "mud.db"
    finally:
        conn.close()


async def test_take_commits_before_the_event_is_published(stored):
    (world, store, db_path) = stored
    seen: list[ObjectLocation | None] = []

    async def spy(_event_type, _data):
        seen.append(_object_location(db_path, "coin"))

    await world.engine.subscribe("item_taken", spy)

    result = await execute(world.engine, "Ada", "take coin")

    assert result.events[0].type == "object.taken"
    assert world.coin in world.ada.contents
    assert seen == [ObjectLocation(owner_id=store.player("Ada").id)]


async def test_drop_commits_before_the_event_is_published(stored):
    (world, _store, db_path) = stored
    seen: list[ObjectLocation | None] = []

    async def spy(_event_type, _data):
        seen.append(_object_location(db_path, "lantern"))

    await world.engine.subscribe("item_dropped", spy)

    result = await execute(world.engine, "Ada", "drop lantern")

    assert result.events[0].type == "object.dropped"
    assert world.lantern in world.hall.contents
    assert seen == [ObjectLocation(room_id="hall")]


async def test_move_commits_before_the_event_is_published(stored):
    (world, _store, db_path) = stored
    seen: list[str | None] = []

    async def spy(_event_type, _data):
        seen.append(_player_room(db_path, "Ada"))

    await world.engine.subscribe("player_moved", spy)

    result = await execute(world.engine, "Ada", "go north")

    assert result.events[0].type == "room.state"
    assert world.ada.location is world.yard
    assert seen == ["yard"]


async def test_world_store_reads_what_the_player_owns_and_what_the_room_holds(
    stored,
):
    (world, _store, db_path) = stored

    await execute(world.engine, "Ada", "take coin")

    conn = open_database(db_path)
    try:
        items = PlayerStore(conn, "Ada")
        world_store = WorldStore(conn)
        # Only mutations are written; Ada's starting lantern comes from the
        # seed that #64 owns, so the coin is the one durable row.
        assert items.contents() == ("coin",)
        assert items.room_id == "hall"
        assert world_store.room_contents("hall") == ()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "command",
    ["take coin", "drop lantern", "go north"],
)
async def test_a_failed_write_returns_internal_and_leaves_memory_alone(
    tmp_path, command
):
    def disk_is_full(_op: str) -> None:
        raise OSError("disk is full")

    conn = open_database(tmp_path / "mud.db")
    try:
        store = WriteThroughStore(conn, fail=disk_is_full)
        world = _world(store)
        published: list[tuple[str, dict]] = []

        async def spy(event_type, data):
            published.append((event_type, data))

        for event_type in ("item_taken", "item_dropped", "player_moved"):
            await world.engine.subscribe(event_type, spy)
        before = {
            "hall": list(world.hall.contents),
            "ada": list(world.ada.contents),
            "room": world.ada.location,
        }

        result = await execute(world.engine, "Ada", command)

        assert [(event.type, event.data.get("code")) for event in result.events] == [
            ("error", "internal"),
        ]
        assert published == []
        assert list(world.hall.contents) == before["hall"]
        assert list(world.ada.contents) == before["ada"]
        assert world.ada.location is before["room"]
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 0
    finally:
        conn.close()


async def test_a_real_sqlite_failure_rolls_back_and_changes_nothing(tmp_path):
    conn = open_database(tmp_path / "mud.db")
    try:
        store = WriteThroughStore(conn)
        world = _world(store)
        published: list[str] = []

        async def spy(event_type, _data):
            published.append(event_type)

        await world.engine.subscribe("item_taken", spy)

        # A read-only connection makes every write fail the way a full or
        # locked disk would, without needing the injection seam.
        conn.execute("PRAGMA query_only = ON")
        # The string seam reports the failed commit as a reply; the v2 adapter
        # turns the same outcome into a typed `error{internal}`.
        reply = await world.engine.process_command("take coin", "Ada")
        conn.execute("PRAGMA query_only = OFF")

        assert "could not save" in reply.lower()
        assert published == []
        assert world.coin in world.hall.contents
        assert world.coin not in world.ada.contents
        assert conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 0

        result = await execute(world.engine, "Ada", "take coin")
        assert result.events[0].type == "object.taken"
        assert _object_location(tmp_path / "mud.db", "coin") is not None
    finally:
        conn.close()


async def test_v2_server_writes_through_before_the_client_sees_it(tmp_path):
    db_path = tmp_path / "server.db"
    world = _world()
    server = V2Server(
        world.engine, port=0, db_path=db_path, auth_required=False)
    task = asyncio.create_task(server.run())
    try:
        for _ in range(300):
            if server.port:
                break
            await asyncio.sleep(0.01)
        assert server.port, "v2 server did not bind"

        async with websockets.connect(f"ws://localhost:{server.port}") as ws:
            await ws.recv()  # hello
            await ws.send(json.dumps({
                # The fixture already holds Ada, so a fresh client is Ben.
                "v": 2, "type": "join", "id": "join",
                "data": {"name": "Ben"},
            }))
            while json.loads(await asyncio.wait_for(ws.recv(), 3))["type"] != "room.state":
                pass

            await ws.send(json.dumps({
                "v": 2, "type": "input", "id": 7,
                "data": {"text": "take coin"},
            }))
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), 3))
                if message.get("type") == "object.taken":
                    break

            # The event arrived on the wire; the row must already be committed.
            assert _object_location(db_path, "coin") is not None
    finally:
        server.stop()
        await asyncio.wait_for(task, timeout=3.0)
    assert server.store is None


CHILD_SERVER = """
import asyncio
import sys

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server


def build_engine():
    engine = Engine()
    hall = Room("Hall", "A stone hall.")
    hall.item_id = "hall"
    yard = Room("Yard", "A muddy yard.")
    yard.item_id = "yard"
    hall.exits["north"] = yard
    coin = Entity("Coin", "A gold coin.", item_id="coin")
    coin.instance_id = "coin"
    lantern = Entity("Lantern", "A brass lantern.", item_id="lantern")
    lantern.instance_id = "lantern"
    hall.add_content(coin)
    hall.add_content(lantern)
    engine._rooms[hall.name] = hall
    engine._rooms[yard.name] = yard
    engine.start_room = hall
    return engine


async def main(path):
    server = V2Server(build_engine(), port=0, db_path=path,
                      auth_required=True)
    task = asyncio.create_task(server.run())
    while not server.port:
        await asyncio.sleep(0.01)
    print(server.port, flush=True)
    await task


asyncio.run(main(sys.argv[1]))
"""


async def _play(port: int) -> None:
    """Take, drop, and move over the wire, waiting for each reply.

    Registers a durable character rather than joining as a guest: #189 makes a
    guest spill its inventory on disconnect, which would move the items before
    the SIGKILL and hide the take/move durability under test.
    """
    async with websockets.connect(f"ws://localhost:{port}") as ws:
        await ws.recv()  # hello
        await ws.send(json.dumps({
            "v": 2, "type": "auth", "id": "join",
            "data": {"mode": "register", "name": "Ada"},
        }))
        while json.loads(await asyncio.wait_for(ws.recv(), 5))["type"] != "room.state":
            pass

        commands = ["take lantern", "take coin", "drop lantern", "go north"]
        for msg_id, text in enumerate(commands, 1):
            await ws.send(json.dumps({
                "v": 2, "type": "input", "id": msg_id,
                "data": {"text": text},
            }))
            # Every command's last direct event echoes its id, so this is the
            # point at which the client has seen the mutation.
            while json.loads(await asyncio.wait_for(ws.recv(), 5)).get("id") != msg_id:
                pass


def test_take_drop_and_move_survive_sigkill(tmp_path):
    db_path = tmp_path / "kill.db"
    script = tmp_path / "child_server.py"
    script.write_text(CHILD_SERVER, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))

    child = subprocess.Popen(
        [sys.executable, str(script), str(db_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        ready, _, _ = select.select([child.stdout], [], [], 20)
        if not ready:
            raise AssertionError(
                f"child server did not start: {child.stderr.read()}")
        port_line = child.stdout.readline().strip()
        assert port_line.isdigit(), child.stderr.read()
        asyncio.run(_play(int(port_line)))

        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    conn = open_database(db_path)
    try:
        world = WorldStore(conn)
        ada = PlayerStore(conn, "Ada")
        coin = world.object_location("coin")
        lantern = world.object_location("lantern")
        assert coin == ObjectLocation(owner_id=ada.id)
        assert lantern == ObjectLocation(room_id="hall")
        assert ada.room_id == "yard"
    finally:
        conn.close()
