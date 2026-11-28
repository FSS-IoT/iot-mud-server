"""Ticket #131: every world mutation crosses one durable boundary.

`put` and `get <item> from <container>` used to move items only in memory, so a
restart put them back where the YAML authored them. The store now takes one
`ChangeSet` per command (placements and flags) and commits it before the
engine changes memory. Lock, lid, and light state follow the owner's
2026-11-26 decision: durable and shared, stored with the item or container.

These tests fail on the parent commit: the database never hears about a
container move, an open lid, a turned key, or a lit torch.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from src.mud.engine.core import Engine
from src.mud.protocol.adapter import execute
from src.mud.storage.db import open_database
from src.mud.storage.store import (
    ObjectLocation,
    PlayerStore,
    WorldStore,
    WriteThroughStore,
)
from src.mud.world.loader import load_world
from src.mud.world.seed import seed_world
from tests.golden.harness import HarnessClient, server_harness

WORLD = """
name: Container Test
start_room: hall
rng_seed: 7
rooms:
  - id: hall
    name: Hall
    description: A stone hall.
    exits:
      north:
        to: vault
        locked: true
        key: key_brass
        label: brass door
    objects:
      - id: brazier
        name: Brazier
        description: A small coal brazier, still warm.
        weight: 4.0
        portable: false
        light_source: true
        lit: true
      - id: coin
        name: Coin
        description: A gold coin.
        weight: 0.1
      - id: key_brass
        name: Brass Key
        description: A small brass key.
        weight: 0.1
      - id: chest
        name: Chest
        description: A sturdy chest.
        kind: container
        weight: 5.0
        contains:
          - lantern
      - id: basket
        name: Basket
        description: A woven basket.
        kind: container
        weight: 0.5
        state:
          open: true
      - id: lantern
        name: Lantern
        description: A brass lantern.
        weight: 0.4
      - id: torch
        name: Torch
        description: A wooden torch.
        weight: 0.5
        light_source: true
  - id: vault
    name: Vault
    description: A dusty vault.
"""


def _world_file(tmp_path: Path) -> Path:
    path = tmp_path / "world.yaml"
    path.write_text(WORLD, encoding="utf-8")
    return path


def _seed(db_path: Path, world_path: Path) -> None:
    conn = open_database(db_path)
    try:
        seed_world(conn, world_path, epoch="container-persistence")
    finally:
        conn.close()


@asynccontextmanager
async def _running(world_path: Path, db_path: Path):
    """A real server over a freshly loaded world and the same database."""
    engine = Engine()
    await load_world(engine, world_path)
    async with server_harness(
        engine, db_path=db_path, auth_required=True, tick_seconds=3600,
    ) as server:
        yield server


def _location(db_path: Path, key: str) -> ObjectLocation | None:
    conn = open_database(db_path)
    try:
        return WorldStore(conn).object_location(key)
    finally:
        conn.close()


async def test_put_then_restart_restores_the_container_location(tmp_path):
    world_path = _world_file(tmp_path)
    db_path = tmp_path / "mud.db"
    _seed(db_path, world_path)

    async with _running(world_path, db_path) as server, HarnessClient(server) as ada:
        key = await ada.register_key("Ada")
        await ada.input("take coin", 2)
        await ada.next("object.taken", msg_id=2)
        await ada.input("put coin in chest", 3)
        await ada.next("object.put", msg_id=3)

    assert _location(db_path, "coin") == ObjectLocation(container_id="chest")

    async with _running(world_path, db_path) as server, HarnessClient(server) as ada:
        await ada.login(key, "login")

        inventory = {item["id"] for item in await ada.inventory()}
        assert "coin" not in inventory

        coin = server.engine.index.object_by_id("coin")
        chest = server.engine.index.object_by_id("chest")
        assert coin in chest.contents
        assert coin not in server.engine.index.room_by_id("hall").contents


async def test_get_from_then_restart_restores_the_inventory(tmp_path):
    world_path = _world_file(tmp_path)
    db_path = tmp_path / "mud.db"
    _seed(db_path, world_path)

    async with _running(world_path, db_path) as server, HarnessClient(server) as ada:
        key = await ada.register_key("Ada")
        await ada.input("open chest", 2)
        await ada.next("object.opened", msg_id=2)
        await ada.input("get lantern from chest", 3)
        await ada.next("object.got", msg_id=3)

    conn = open_database(db_path)
    try:
        ada_id = PlayerStore(conn, "Ada").id
        assert WorldStore(conn).object_location("lantern") == ObjectLocation(
            owner_id=ada_id)
    finally:
        conn.close()

    async with _running(world_path, db_path) as server, HarnessClient(server) as ada:
        await ada.login(key, "login")

        inventory = {item["id"] for item in await ada.inventory()}
        assert "lantern" in inventory

        lantern = server.engine.index.object_by_id("lantern")
        chest = server.engine.index.object_by_id("chest")
        assert lantern not in chest.contents


async def test_open_light_and_unlock_survive_a_restart_and_stay_shared(
    tmp_path,
):
    world_path = _world_file(tmp_path)
    db_path = tmp_path / "mud.db"
    _seed(db_path, world_path)

    async with _running(world_path, db_path) as server, HarnessClient(server) as ada:
        key = await ada.register_key("Ada")
        await ada.input("open chest", 2)
        await ada.next("object.opened", msg_id=2)
        await ada.input("take torch", 3)
        await ada.next("object.taken", msg_id=3)
        await ada.input("light torch", 4)
        await ada.next("object.lit")
        await ada.input("take brass key", 5)
        await ada.next("object.taken", msg_id=5)
        await ada.input("use brass key on north", 6)
        await ada.next("exit.unlocked", msg_id=6)

        assert server.engine.index.object_by_id("chest").open is True
        assert server.engine.index.object_by_id("torch").lit is True
        hall = server.engine.index.room_by_id("hall")
        vault = server.engine.index.room_by_id("vault")
        assert hall.exit_locks["north"].locked is False
        assert vault.exit_locks["south"] is hall.exit_locks["north"]

    # The same durable flags are visible to the next process, not re-authored.
    async with _running(world_path, db_path) as server, HarnessClient(server) as ada:
        await ada.login(key, "login")

        chest = server.engine.index.object_by_id("chest")
        torch = server.engine.index.object_by_id("torch")
        hall = server.engine.index.room_by_id("hall")
        vault = server.engine.index.room_by_id("vault")
        assert chest.open is True
        assert torch.lit is True
        assert hall.exit_locks["north"].locked is False
        assert vault.exit_locks["south"].locked is False


async def test_close_writes_the_lid_back_to_sqlite(tmp_path):
    world_path = _world_file(tmp_path)
    db_path = tmp_path / "mud.db"
    _seed(db_path, world_path)

    async with _running(world_path, db_path) as server, HarnessClient(server) as ada:
        key = await ada.register_key("Ada")
        await ada.input("close basket", 2)
        await ada.next("object.closed", msg_id=2)

    async with _running(world_path, db_path) as server, HarnessClient(server) as ada:
        await ada.login(key, "login")
        assert server.engine.index.object_by_id("basket").open is False


async def test_a_failed_container_commit_leaves_memory_and_the_rows_alone(
    tmp_path,
):
    world_path = _world_file(tmp_path)
    db_path = tmp_path / "mud.db"
    _seed(db_path, world_path)

    def fail(op: str) -> None:
        if op in ("put", "get"):
            raise OSError("disk is full")

    conn = open_database(db_path)
    try:
        store = WriteThroughStore(conn, fail=fail)
        engine = Engine(store=store)
        await load_world(engine, world_path)
        engine.register_player("Ada")

        await execute(engine, "Ada", "take coin")
        before = _location(db_path, "coin")

        result = await execute(engine, "Ada", "put coin in chest")

        assert [(event.type, event.data.get("code")) for event in result.events] == [
            ("error", "internal"),
        ]
        coin = engine.index.object_by_id("coin")
        chest = engine.index.object_by_id("chest")
        ada = engine.get_player("Ada")
        assert coin in ada.contents
        assert coin not in chest.contents
        assert _location(db_path, "coin") == before

        await execute(engine, "Ada", "open chest")
        lantern = engine.index.object_by_id("lantern")
        result = await execute(engine, "Ada", "get lantern from chest")

        assert [(event.type, event.data.get("code")) for event in result.events] == [
            ("error", "internal"),
        ]
        assert lantern in chest.contents
        assert lantern not in ada.contents
        assert _location(db_path, "lantern") == ObjectLocation(
            container_id="chest")
    finally:
        conn.close()
