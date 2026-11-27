"""Ticket #71: a character's room and inventory survive a restart.

SPEC v2.1.1 criterion 4. Movement and take already commit to SQLite before
their events (#63), and login already reads the character's room back (#69).
This ticket adds the inventory half: login asks the store which objects the
character owns, finds those objects in the freshly loaded world, and puts them
back in the character's hands.

The hard-restart test drives a real v2 server in a child process, kills it with
SIGKILL after the client has seen the take, and starts a new server against the
same database. Nothing is flushed at shutdown, so what survives is only what
the store committed.
"""

from __future__ import annotations

import asyncio
import os
import select
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine
from src.mud.storage.db import open_database
from src.mud.storage.store import CharacterState, PlayerStore, WriteThroughStore
from src.mud.world.loader import load_demo_village
from tests.golden.harness import HarnessClient as Client
from tests.golden.harness import server_harness

REPO_ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def _server(db_path: Path):
    engine = Engine()
    await load_demo_village(engine)
    async with server_harness(
        engine, db_path=db_path, auth_required=True,
    ) as server:
        yield server


async def test_login_after_a_restart_resumes_the_room_and_the_inventory(tmp_path):
    db_path = tmp_path / "restart.db"

    async with _server(db_path) as server, Client(server) as ada:
        key = await ada.register_key("Ada", "c-1")

        await ada.input("go north", 2)
        moved = await ada.next("room.state", msg_id=2)
        assert moved["data"]["room"]["id"] == "market"

        await ada.input("take gold coin", 3)
        taken = await ada.next("object.taken", msg_id=3)
        assert taken["data"]["object"]["id"] == "gold_coin"

    # A new engine and a new server over the same database: the world reloads
    # from YAML, so the coin the character took is authored back onto the
    # Marketplace floor until login restores the character's inventory.
    async with _server(db_path) as server, Client(server) as ada:
        ok, state = await ada.login(key, "c-2")

        assert ok["data"]["player"] == {"name": "Ada"}
        assert state["data"]["room"]["id"] == "market"
        assert state["data"]["room"]["name"] == "Marketplace"
        assert state["data"]["room"]["objects"] == []

        assert await ada.inventory() == [
            {"id": "gold_coin", "name": "Gold Coin", "weight": 0.01},
        ]


def test_player_store_load_reads_the_durable_room_and_inventory(tmp_path):
    conn = open_database(tmp_path / "mud.db")
    try:
        store = WriteThroughStore(conn)
        hall = Room("Hall", "A stone hall.")
        hall.item_id = "hall"
        yard = Room("Yard", "A muddy yard.")
        yard.item_id = "yard"
        coin = Entity("Coin", "A gold coin.", item_id="coin")
        coin.instance_id = "coin"

        store.player("Ada").move(room=yard)
        store.player("Ada").take(coin, room=yard)

        assert PlayerStore(conn, "Ada").load() == CharacterState(
            room_id="yard", items=("coin",))
    finally:
        conn.close()


def test_restore_player_takes_owned_objects_off_the_floor_and_out_of_containers():
    engine = Engine()
    hall = Room("Hall", "A stone hall.")
    hall.item_id = "hall"
    chest = Entity("Chest", "A sturdy chest.", item_id="chest")
    chest.instance_id = "chest"
    coin = Entity("Coin", "A gold coin.", item_id="coin")
    coin.instance_id = "coin"
    lantern = Entity("Lantern", "A brass lantern.", item_id="lantern")
    lantern.instance_id = "lantern"
    chest.add_content(lantern)
    hall.add_content(chest)
    hall.add_content(coin)
    engine._rooms[hall.name] = hall
    engine.start_room = hall

    ada = engine.register_player("Ada")
    engine.restore_player(ada, room=hall, item_keys=("coin", "lantern"))

    assert coin in ada.contents
    assert lantern in ada.contents
    assert coin not in hall.contents
    assert lantern not in chest.contents


CHILD_SERVER = """
import asyncio
import sys

from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.world.loader import load_demo_village


async def main(path):
    engine = Engine()
    await load_demo_village(engine)
    server = V2Server(engine, port=0, db_path=path, auth_required=True)
    port = await server.start()
    print(port, flush=True)
    await asyncio.Event().wait()


asyncio.run(main(sys.argv[1]))
"""


def _start_child(script: Path, db_path: Path):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    child = subprocess.Popen(
        [sys.executable, str(script), str(db_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    ready, _, _ = select.select([child.stdout], [], [], 20)
    if not ready:
        child.kill()
        child.wait(timeout=10)
        raise AssertionError(
            f"child server did not start: {child.stderr.read()}")
    port_line = child.stdout.readline().strip()
    if not port_line.isdigit():
        child.kill()
        child.wait(timeout=10)
        raise AssertionError(
            f"child server did not report a port: {port_line!r} "
            f"{child.stderr.read()}")
    return child, int(port_line)


async def _take_the_coin(port: int) -> str:
    """Register, move to the market, take the coin; return the key."""
    async with Client(f"ws://localhost:{port}") as ada:
        key = await ada.register_key("Ada", "c-1")

        await ada.input("go north", 2)
        moved = await ada.next("room.state", msg_id=2)
        assert moved["data"]["room"]["id"] == "market"

        await ada.input("take gold coin", 3)
        taken = await ada.next("object.taken", msg_id=3)
        assert taken["data"]["object"]["id"] == "gold_coin"
        return key


async def _log_in_and_read(port: int, key: str):
    """Log in with the old key and return (room id, inventory)."""
    async with Client(f"ws://localhost:{port}") as ada:
        ok, state = await ada.login(key, "c-2")
        assert ok["data"]["player"] == {"name": "Ada"}
        return state["data"]["room"]["id"], await ada.inventory()


def test_character_room_and_inventory_survive_sigkill(tmp_path):
    db_path = tmp_path / "kill.db"
    script = tmp_path / "child_server.py"
    script.write_text(CHILD_SERVER, encoding="utf-8")

    first, port = _start_child(script, db_path)
    try:
        key = asyncio.run(_take_the_coin(port))
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=10)
        assert first.returncode == -signal.SIGKILL
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10)

    second, port = _start_child(script, db_path)
    try:
        room_id, inventory = asyncio.run(_log_in_and_read(port, key))
        assert room_id == "market"
        assert inventory == [
            {"id": "gold_coin", "name": "Gold Coin", "weight": 0.01}]
        os.kill(second.pid, signal.SIGKILL)
        second.wait(timeout=10)
    finally:
        if second.poll() is None:
            second.kill()
            second.wait(timeout=10)
