"""Ticket #72: disconnect sleeps the character; login wakes it in place.

SPEC v2.1.1 criterion 5, as revised by the owner on 2026-11-22:

* An authenticated disconnect keeps the character in its room and marks it
  asleep and stone (present and invincible) with `players.asleep` and
  `players.last_seen`.
* Items marked `dropped_on_sleep: true` drop into the room through the
  write-through store so another player can pick them up. The gold chest key
  carries the flag; the coin, lantern, and everything else stay in the sleeping
  character's inventory (#71).
* `who.list` and `room.state` player entries show the sleep/stone state.
* Login wakes the character in place, clears stone, and restores the remaining
  inventory. No offline queue is created.

The guest path on an explicit `auth_required=False` server keeps the old
remove-on-disconnect behavior.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Self

import pytest
import websockets

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.storage.db import open_database
from src.mud.storage.store import (
    PlayerStore,
    StorageWriteError,
    WriteThroughStore,
)
from src.mud.world.loader import load_demo_village, load_world
from src.mud.world.seed import seed_world


class Client:
    """A tiny v2 client with a background reader and a type-filtered drain."""

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

    async def join(self, name: str):
        await self.send({
            "v": 2, "type": "join", "id": "join",
            "data": {"name": name},
        })
        ok = await self.next("session.ok")
        state = await self.next("room.state")
        return ok, state

    async def input(self, text: str, msg_id=1) -> None:
        await self.send({
            "v": 2, "type": "input", "id": msg_id,
            "data": {"text": text},
        })

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

    async def inventory(self, msg_id="inv") -> list[dict]:
        await self.input("inventory", msg_id)
        event = await self.next("player.inventory", msg_id=msg_id)
        return event["data"]["items"]

    async def who(self, msg_id="who") -> dict:
        await self.input("who", msg_id)
        return await self.next("who.list", msg_id=msg_id)

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


async def _start(db_path):
    engine = Engine()
    await load_demo_village(engine)
    instance = V2Server(
        engine, port=0, db_path=db_path, auth_required=True)
    task = asyncio.create_task(instance.run())
    await _wait_for_bind(instance)
    return instance, engine, task


async def _stop(instance: V2Server, task) -> None:
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


async def _take_the_key_in_shop(client: Client, first_id: int = 2) -> None:
    """Walk west from the marketplace to the shop and take the gold key."""
    await client.input("go west", first_id)
    shop = await client.next("room.state", msg_id=first_id)
    assert shop["data"]["room"]["id"] == "shop"
    await client.input("take gold key", first_id + 1)
    taken = await client.next("object.taken", msg_id=first_id + 1)
    assert taken["data"]["object"]["id"] == "key_gold_1"


@pytest.mark.asyncio
async def test_disconnect_sleeps_in_place_and_drops_flagged_items(tmp_path):
    db_path = tmp_path / "sleep.db"

    async with auth_server(db_path) as (instance, engine):
        uri = f"ws://localhost:{instance.port}"

        ada = await Client(uri).connect()
        key = await ada.register("Ada", "c-1")

        await ada.input("go north", 2)
        await ada.next("room.state", msg_id=2)
        await ada.input("take gold coin", 3)
        await ada.next("object.taken", msg_id=3)
        await _take_the_key_in_shop(ada, 4)
        assert {item["id"] for item in await ada.inventory()} == {
            "gold_coin", "key_gold_1",
        }

        ben = await Client(uri).connect()
        await ben.register("Ben", "c-2")
        await ben.input("go north", 2)
        await ben.next("room.state", msg_id=2)
        await ben.input("go west", 3)
        await ben.next("room.state", msg_id=3)
        await ada.next("room.enter")  # Ben arrives in the shop

        # Ada's socket closes without a quit command: a disconnect.
        await ada.close()

        left = await ben.next("room.leave")
        assert left["data"]["actor"]["name"] == "Ada"
        assert left["data"]["reason"] == "disconnect"
        assert left["data"]["asleep"] is True
        assert left["data"]["stone"] is True
        # Only the gold key carries `dropped_on_sleep`. The coin stays.
        assert left["data"]["dropped"] == [
            {"id": "key_gold_1", "name": "Gold Key"},
        ]

        # The engine kept the character in the room, asleep/stone, carrying
        # the coin. The key is on the shop floor.
        sleeping = engine.get_player("Ada")
        assert sleeping is not None
        assert sleeping.asleep is True
        shop = engine.get_room("Alchemist Shop")
        assert sleeping.location is shop
        assert [item.name for item in sleeping.contents] == ["Gold Coin"]
        assert "Gold Key" in [item.name for item in shop.contents]
        gold_key = next(
            item for item in shop.contents if item.item_id == "key_gold_1")
        assert gold_key.dropped_on_sleep is True

        # The durable half agrees: asleep + last_seen, the key on the floor,
        # the coin still owned by Ada.
        conn = open_database(db_path)
        try:
            row = conn.execute(
                "SELECT id, room_id, asleep, last_seen FROM players "
                "WHERE name = 'Ada'"
            ).fetchone()
            assert row["room_id"] == "shop"
            assert row["asleep"] == 1
            assert row["last_seen"]
            key_row = conn.execute(
                "SELECT room_id, owner_id FROM objects WHERE id = 'key_gold_1'"
            ).fetchone()
            assert key_row["room_id"] == "shop"
            assert key_row["owner_id"] is None
            coin_row = conn.execute(
                "SELECT room_id, owner_id FROM objects WHERE id = 'gold_coin'"
            ).fetchone()
            assert coin_row["room_id"] is None
            assert coin_row["owner_id"] == row["id"]
        finally:
            conn.close()

        # The room sees the stone occupant and the dropped key, not the coin.
        await ben.input("look", 4)
        state = await ben.next("room.state", msg_id=4)
        assert {"name": "Ada", "asleep": True, "stone": True} in (
            state["data"]["room"]["players"])
        object_ids = {obj["id"] for obj in state["data"]["room"]["objects"]}
        assert "key_gold_1" in object_ids
        assert "gold_coin" not in object_ids

        # `who` lists everyone server-wide, awake and asleep.
        who = await ben.who("who-1")
        players = {entry["name"]: entry for entry in who["data"]["players"]}
        assert players["Ada"] == {
            "name": "Ada", "room_id": "shop", "asleep": True, "stone": True,
        }
        assert players["Ben"] == {
            "name": "Ben", "room_id": "shop", "asleep": False, "stone": False,
        }
        assert "asleep" in who["data"]["text"].lower()

        # Login wakes Ada in place with the coin that stayed with her, and the
        # room sees her wake. The dropped key is still on the floor.
        ada = await Client(uri).connect()
        ok, state = await ada.login(key, "c-3")
        assert ok["data"]["player"] == {"name": "Ada"}
        assert state["data"]["room"]["id"] == "shop"
        assert await ada.inventory() == [
            {"id": "gold_coin", "name": "Gold Coin"},
        ]
        assert "key_gold_1" in [
            item.item_id for item in engine.get_room("Alchemist Shop").contents]

        entered = await ben.next("room.enter")
        assert entered["data"]["actor"]["name"] == "Ada"
        assert entered["data"]["reason"] == "connect"

        who = await ada.who("who-2")
        players = {entry["name"]: entry for entry in who["data"]["players"]}
        assert players["Ada"]["asleep"] is False
        assert players["Ada"]["stone"] is False
        assert engine.get_player("Ada").asleep is False

        # Wake creates no offline queue: once the direct replies are read,
        # nothing else arrives from the time Ada was asleep.
        with pytest.raises(asyncio.TimeoutError):
            await ada.next_any(timeout=0.3)

        # Another player really can pick the dropped gold key up, after the
        # wake, from the room where Ada left it.
        await ben.input("take gold key", 6)
        taken = await ben.next("object.taken", msg_id=6)
        assert taken["data"]["object"]["id"] == "key_gold_1"
        await ada.close()


def test_sleep_player_drops_only_flagged_items_and_keeps_the_rest(tmp_path):
    conn = open_database(tmp_path / "engine.db")
    try:
        store = WriteThroughStore(conn)
        engine = Engine(store=store)
        hall = Room("Hall", "A stone hall.")
        hall.item_id = "hall"
        key = Entity(
            "Brass Key", "A small brass key.", item_id="brass_key",
            dropped_on_sleep=True)
        key.instance_id = "brass_key"
        coin = Entity("Coin", "A gold coin.", item_id="coin")
        coin.instance_id = "coin"
        lantern = Entity(
            "Lantern", "A brass lantern.", item_id="lantern")
        lantern.instance_id = "lantern"
        hall.add_content(key)
        hall.add_content(coin)
        hall.add_content(lantern)
        engine._rooms[hall.name] = hall
        engine._player_location = hall

        ada = engine.register_player("Ada")
        for item in (key, coin, lantern):
            store.player("Ada").take(item, room=hall)
            hall.remove_content(item)
            ada.add_content(item)

        dropped = engine.sleep_player("Ada")

        assert [item.name for item in dropped] == ["Brass Key"]
        assert key in hall.contents
        assert key not in ada.contents
        # Only `dropped_on_sleep` leaves; everything else stays with Ada.
        assert coin in ada.contents
        assert lantern in ada.contents
        assert ada.asleep is True
        row = conn.execute(
            "SELECT id, asleep, last_seen FROM players WHERE name = 'Ada'"
        ).fetchone()
        assert row["asleep"] == 1
        assert row["last_seen"]
        key_row = conn.execute(
            "SELECT room_id, owner_id FROM objects WHERE id = 'brass_key'"
        ).fetchone()
        assert key_row["room_id"] == "hall"
        assert key_row["owner_id"] is None
        for object_id in ("coin", "lantern"):
            kept_row = conn.execute(
                "SELECT room_id, owner_id FROM objects WHERE id = ?",
                (object_id,),
            ).fetchone()
            assert kept_row["room_id"] is None
            assert kept_row["owner_id"] == row["id"]

        entity = engine.wake_player("Ada")
        assert entity is ada
        assert ada.asleep is False
        assert coin in ada.contents
        assert lantern in ada.contents
        woken = conn.execute(
            "SELECT asleep, last_seen FROM players WHERE name = 'Ada'"
        ).fetchone()
        assert woken["asleep"] == 0
        assert woken["last_seen"]
    finally:
        conn.close()


def test_sleep_drop_commits_before_memory_moves(tmp_path):
    conn = open_database(tmp_path / "fail.db")

    def fail(op: str) -> None:
        if op == "drop":
            raise StorageWriteError("disk full")

    try:
        store = WriteThroughStore(conn, fail=fail)
        engine = Engine(store=store)
        hall = Room("Hall", "A stone hall.")
        hall.item_id = "hall"
        key = Entity(
            "Key", "A key that drops on sleep.", item_id="key",
            dropped_on_sleep=True)
        key.instance_id = "key"
        hall.add_content(key)
        engine._rooms[hall.name] = hall
        engine._player_location = hall
        ada = engine.register_player("Ada")
        store.player("Ada").take(key, room=hall)
        hall.remove_content(key)
        ada.add_content(key)

        with pytest.raises(StorageWriteError):
            engine.sleep_player("Ada")

        # The write failed before any in-memory move: the key stays with Ada
        # and she is still awake.
        assert key in ada.contents
        assert key not in hall.contents
        assert ada.asleep is False
        row = conn.execute(
            "SELECT owner_id, room_id FROM objects WHERE id = 'key'"
        ).fetchone()
        player_id = conn.execute(
            "SELECT id FROM players WHERE name = 'Ada'"
        ).fetchone()["id"]
        assert row["owner_id"] == player_id
        assert row["room_id"] is None
    finally:
        conn.close()


DROP_WORLD = """name: Drop Test
start_room: hall
rng_seed: 7
rooms:
  - id: hall
    name: Hall
    description: A bare hall.
    objects:
      - id: coin
        name: Coin
        description: An ordinary coin.
      - id: key
        name: Key
        description: A key that drops on sleep.
        dropped_on_sleep: true
"""


def _drop_world(tmp_path) -> Path:
    path = tmp_path / "drop.yaml"
    path.write_text(DROP_WORLD, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_yaml_flag_drops_the_key_and_keeps_the_coin(tmp_path):
    engine = Engine()
    await load_world(engine, _drop_world(tmp_path))
    hall = engine.get_room("Hall")
    coin = next(item for item in hall.contents if item.item_id == "coin")
    key = next(item for item in hall.contents if item.item_id == "key")
    assert key.dropped_on_sleep is True
    assert coin.dropped_on_sleep is False

    ada = engine.register_player("Ada")
    hall.remove_content(coin)
    hall.remove_content(key)
    ada.add_content(coin)
    ada.add_content(key)

    dropped = engine.sleep_player("Ada")

    assert [item.item_id for item in dropped] == ["key"]
    assert key in hall.contents
    assert key not in ada.contents
    assert coin in ada.contents
    assert ada.asleep is True

    engine.wake_player("Ada")
    assert ada.asleep is False
    assert coin in ada.contents
    assert key not in ada.contents


def test_seed_persists_dropped_on_sleep_in_object_state(tmp_path):
    conn = open_database(tmp_path / "drop_seed.db")
    try:
        seed_world(conn, _drop_world(tmp_path))
        key = json.loads(conn.execute(
            "SELECT state FROM objects WHERE id = 'key'"
        ).fetchone()["state"])
        coin = json.loads(conn.execute(
            "SELECT state FROM objects WHERE id = 'coin'"
        ).fetchone()["state"])
        assert key["dropped_on_sleep"] is True
        assert coin["dropped_on_sleep"] is False
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_json_oracle_carries_the_drop_flag():
    """Both world files agree that the gold key drops on sleep."""
    engine = Engine()
    json_world = (
        Path(__file__).resolve().parents[1]
        / "data" / "worlds" / "starting_village.json"
    )
    await load_world(engine, json_world)
    items = [
        item
        for room in engine.rooms.values()
        for item in room.contents
    ]
    gold_key = next(
        item for item in items if getattr(item, "item_id", None) == "key_gold_1")
    coin = next(
        item for item in items if getattr(item, "item_id", None) == "gold_coin")
    assert gold_key.dropped_on_sleep is True
    assert coin.dropped_on_sleep is False


@pytest.mark.asyncio
async def test_who_reads_the_durable_roster_after_a_restart(tmp_path):
    db_path = tmp_path / "restart.db"

    async with auth_server(db_path) as (instance, _engine), \
            Client(f"ws://localhost:{instance.port}") as ada:
        key = await ada.register("Ada", "c-1")
        await ada.input("go north", 2)
        await ada.next("room.state", msg_id=2)
        await _take_the_key_in_shop(ada, 3)

    # A new server and engine: Ada is not connected, but her character row is
    # asleep in the shop. `who` is a server-wide roster, not just live sockets.
    async with auth_server(db_path) as (instance, engine):
        async with Client(f"ws://localhost:{instance.port}") as carla:
            await carla.register("Carla", "c-2")
            who = await carla.who("who-1")
            players = {entry["name"]: entry for entry in who["data"]["players"]}
            assert players["Ada"] == {
                "name": "Ada", "room_id": "shop", "asleep": True,
                "stone": True,
            }
            assert players["Carla"]["asleep"] is False
            assert engine.get_player("Ada") is None

        # Logging in after the restart wakes Ada in place. The key was dropped
        # into the shop before the server stopped, so nothing is carried.
        async with Client(f"ws://localhost:{instance.port}") as ada:
            ok, state = await ada.login(key, "c-3")
            assert ok["data"]["player"] == {"name": "Ada"}
            assert state["data"]["room"]["id"] == "shop"
            assert await ada.inventory() == []
            assert engine.get_player("Ada").asleep is False


@pytest.mark.asyncio
async def test_guest_disconnect_still_removes_the_player():
    engine = Engine()
    await load_demo_village(engine)
    instance = V2Server(engine, port=0, auth_required=False)
    task = asyncio.create_task(instance.run())
    await _wait_for_bind(instance)
    try:
        async with Client(f"ws://localhost:{instance.port}") as guest:
            await guest.join("Ada")
        for _ in range(200):
            if engine.get_player("Ada") is None:
                break
            await asyncio.sleep(0.01)
        assert engine.get_player("Ada") is None
    finally:
        await _stop(instance, task)


def test_player_store_sleep_and_wake_record_the_state(tmp_path):
    conn = open_database(tmp_path / "store.db")
    try:
        hall = Room("Hall", "A stone hall.")
        hall.item_id = "hall"
        store = PlayerStore(conn, "Ada")
        store.move(room=hall)

        store.sleep(room=hall)
        sleeping = conn.execute(
            "SELECT room_id, asleep, last_seen FROM players "
            "WHERE name = 'Ada'"
        ).fetchone()
        assert sleeping["room_id"] == "hall"
        assert sleeping["asleep"] == 1
        assert sleeping["last_seen"]

        store.wake()
        awake = conn.execute(
            "SELECT asleep, last_seen FROM players WHERE name = 'Ada'"
        ).fetchone()
        assert awake["asleep"] == 0
        assert awake["last_seen"]
    finally:
        conn.close()
