"""Ticket #64: seed, reset, and world epoch.

`admin seed` writes the effective YAML world into the SQLite database. It seeds
a clean database and refuses a database that already holds a world unless the
caller explicitly asks for a reset. A reset replaces the world, keeps every
character and every character key, puts all characters back at the new
`start_room`, clears their inventory, and changes `world_meta.epoch`.

The v2 server reports that stored epoch in `hello`, `session.ok`, and `auth.ok`
so a client can detect a world reset and drop cached maps.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import websockets

from src.mud.admin import main
from src.mud.auth import register_character
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server, read_world_epoch
from src.mud.storage.db import SCHEMA_VERSION, open_database
from src.mud.world.loader import load_demo_village

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _query(path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _one(path: Path, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    rows = _query(path, sql, params)
    return rows[0] if rows else None


def _meta(path: Path) -> dict[str, str]:
    return {
        row["key"]: row["value"]
        for row in _query(path, "SELECT key, value FROM world_meta")
    }


def _count(path: Path, table: str) -> int:
    return int(_one(path, f"SELECT COUNT(*) AS n FROM {table}")["n"])


def _table(path: Path, table: str) -> list[dict]:
    return [dict(row) for row in _query(path, f"SELECT * FROM {table} ORDER BY 1")]


def _write_custom_world(path: Path) -> Path:
    path.write_text(
        """name: Locked Rooms
start_room: hall
rng_seed: 99
rooms:
  - id: hall
    name: Hall
    description: A bare hall.
    exits:
      north:
        to: vault
        locked: true
        key: brass_key
        label: iron door
      down:
        to: cellar
        one_way: true
    objects:
      - id: brass_key
        name: Brass Key
        description: A small brass key.
      - id: note
        name: Note
        description: A folded note.
        kind: readable
        readable_text: The vault lies north.
  - id: vault
    name: Vault
    description: A cold vault.
    dark: true
    objects:
      - id: chest
        name: Iron Chest
        description: A heavy iron chest.
        kind: container
        locked: true
        required_key: brass_key
        state:
          loot_tier: 2
  - id: cellar
    name: Cellar
    description: A damp cellar.
    npcs:
      - id: keeper
        name: Keeper
        description: A quiet keeper.
        wander: true
        wander_every: 5
        dialogue:
          greeting: Welcome, traveller.
          default: I know little.
          topics:
            vault: The vault is north of the hall.
""",
        encoding="utf-8",
    )
    return path


async def _wait_for_bind(server: V2Server) -> None:
    for _ in range(200):
        if server.port:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("v2 server did not bind")


# ---------------------------------------------------------------------------
# A clean database gets the full YAML world
# ---------------------------------------------------------------------------


def test_seed_fills_a_clean_database_with_the_yaml_world(tmp_path):
    path = tmp_path / "mud.db"

    assert main(["seed", "--db", str(path)]) == 0

    assert _count(path, "rooms") == 5
    assert _count(path, "objects") == 7
    assert _count(path, "exits") == 8  # four declared, four reciprocals
    assert _count(path, "npcs") == 0

    meta = _meta(path)
    assert set(meta) == {"name", "start_room", "rng_seed", "epoch"}
    assert meta["name"] == "Starting Village"
    assert meta["start_room"] == "gate"
    assert meta["rng_seed"] == "20261121"
    assert meta["epoch"]


def test_seed_persists_room_text_dark_and_geometry(tmp_path):
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path)]) == 0

    gate = _one(path, "SELECT * FROM rooms WHERE id = 'gate'")
    assert gate["name"] == "Village Gate"
    assert gate["dark"] == 0
    geometry = json.loads(gate["geometry"])
    assert geometry["cells"][0] == "###+###"
    assert geometry["spawn"]["north"] == [3, 1]
    assert {"kind": "gatepost", "cell": [1, 5]} in geometry["props"]

    forest = _one(path, "SELECT * FROM rooms WHERE id = 'forest'")
    assert forest["dark"] == 1


def test_seed_persists_exits_including_reciprocals(tmp_path):
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path)]) == 0

    gate_north = _one(
        path,
        "SELECT * FROM exits WHERE room_id = 'gate' AND direction = 'north'",
    )
    assert gate_north["target_id"] == "market"

    market_south = _one(
        path,
        "SELECT * FROM exits WHERE room_id = 'market' AND direction = 'south'",
    )
    assert market_south["target_id"] == "gate"

    directions = {
        row["direction"]
        for row in _query(
            path, "SELECT direction FROM exits WHERE room_id = 'market'"
        )
    }
    assert directions == {"north", "south", "east", "west"}


def test_seed_persists_object_placement_kind_and_readable_text(tmp_path):
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path)]) == 0

    sign = _one(path, "SELECT * FROM objects WHERE id = 'sign'")
    assert sign["kind"] == "readable"
    assert sign["room_id"] == "gate"
    assert sign["portable"] == 1
    assert "Marketplace" in sign["readable_text"]

    coin = _one(path, "SELECT * FROM objects WHERE id = 'gold_coin'")
    assert coin["room_id"] == "market"
    assert coin["owner_id"] is None

    chest = _one(path, "SELECT * FROM objects WHERE id = 'chest_forest'")
    assert chest["kind"] == "container"
    assert chest["room_id"] == "forest"

    lantern = _one(path, "SELECT * FROM objects WHERE id = 'brass_lantern'")
    assert lantern["container_id"] == "chest_forest"
    assert lantern["room_id"] is None
    assert lantern["owner_id"] is None


def test_seed_keeps_schema_less_object_fields_in_state(tmp_path):
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path)]) == 0

    torch = json.loads(
        _one(path, "SELECT state FROM objects WHERE id = 'torch'")["state"]
    )
    assert torch["weight"] == 0.5
    assert torch["light_source"] is True
    assert torch["lit"] is False
    assert torch["locked"] is False

    chest = json.loads(
        _one(path, "SELECT state FROM objects WHERE id = 'chest_forest'")["state"]
    )
    assert chest["weight"] == 5.0
    assert chest["locked"] is True
    assert chest["required_key"] == "key_gold_1"


def test_seed_uses_the_config_storage_path(tmp_path):
    config = tmp_path / "mud.toml"
    db = tmp_path / "from-config.db"
    config.write_text(f'[storage]\npath = "{db}"\n', encoding="utf-8")

    assert main(["seed", "--config", str(config)]) == 0

    assert db.exists()
    assert _meta(db)["name"] == "Starting Village"
    assert _one(db, "PRAGMA user_version")[0] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Refusals: a seeded world is not overwritten silently
# ---------------------------------------------------------------------------


def test_seed_refuses_a_second_seed_without_reset(tmp_path, capsys):
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path)]) == 0
    epoch = _meta(path)["epoch"]

    capsys.readouterr()
    assert main(["seed", "--db", str(path)]) == 1

    captured = capsys.readouterr()
    assert "--reset --yes" in captured.err
    assert _meta(path)["epoch"] == epoch
    assert _count(path, "rooms") == 5


def test_seed_reset_requires_yes(tmp_path, capsys):
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path)]) == 0
    epoch = _meta(path)["epoch"]

    capsys.readouterr()
    assert main(["seed", "--reset", "--db", str(path)]) == 2

    captured = capsys.readouterr()
    assert "--yes" in captured.err
    assert "inventory" in captured.err
    assert _meta(path)["epoch"] == epoch


def test_seed_yes_without_reset_is_refused(tmp_path, capsys):
    path = tmp_path / "mud.db"

    assert main(["seed", "--yes", "--db", str(path)]) == 2

    captured = capsys.readouterr()
    assert "--reset" in captured.err
    assert not path.exists()


def test_invalid_world_writes_nothing(tmp_path, capsys):
    world = tmp_path / "broken.yaml"
    world.write_text(
        "name: Broken\n"
        "start_room: gate\n"
        "rng_seed: 1\n"
        "rooms:\n"
        "  - id: gate\n"
        "    name: Gate\n"
        "    description: A gate.\n"
        "    exits:\n"
        "      north: nowhere\n",
        encoding="utf-8",
    )
    path = tmp_path / "mud.db"

    assert main(["seed", "--db", str(path), "--world", str(world)]) == 1

    captured = capsys.readouterr()
    assert "unknown room" in captured.err
    assert _count(path, "rooms") == 0
    assert _meta(path) == {}


# ---------------------------------------------------------------------------
# Reset: same world, new epoch, characters kept
# ---------------------------------------------------------------------------


def test_reset_reproduces_the_world_and_changes_the_epoch(tmp_path):
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path)]) == 0
    before_epoch = _meta(path)["epoch"]
    before = {
        table: _table(path, table)
        for table in ("rooms", "exits", "objects", "npcs")
    }
    before_meta = {
        key: value for key, value in _meta(path).items() if key != "epoch"
    }

    assert main(["seed", "--reset", "--yes", "--db", str(path)]) == 0

    after = {
        table: _table(path, table)
        for table in ("rooms", "exits", "objects", "npcs")
    }
    assert after == before
    assert _meta(path)["epoch"] != before_epoch
    assert {
        key: value for key, value in _meta(path).items() if key != "epoch"
    } == before_meta


def test_reset_keeps_characters_and_keys_and_replaces_them_at_start(tmp_path):
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path)]) == 0

    conn = open_database(path)
    try:
        market = conn.execute(
            "SELECT id, name, description, dark FROM rooms WHERE id = 'market'"
        ).fetchone()
        room = SimpleNamespace(
            item_id="market",
            name=market["name"],
            description=market["description"],
            dark=bool(market["dark"]),
        )

        registration = register_character(conn, "Ada", start_room=room)
        with conn:
            conn.execute(
                "UPDATE players SET asleep = 1 WHERE id = ?",
                (registration.player_id,),
            )
            conn.execute(
                "UPDATE objects SET owner_id = ?, room_id = NULL "
                "WHERE id = 'gold_coin'",
                (registration.player_id,),
            )
    finally:
        conn.close()

    assert main(["seed", "--reset", "--yes", "--db", str(path)]) == 0

    ada = _one(path, "SELECT * FROM players WHERE name = 'Ada'")
    assert ada is not None
    assert ada["id"] == registration.player_id
    assert ada["room_id"] == "gate"
    # An asleep character is still a character: kept, and placed at the start.
    assert ada["asleep"] == 1

    key = _one(
        path,
        "SELECT * FROM character_keys WHERE player_id = ?",
        (registration.player_id,),
    )
    assert key is not None
    assert key["key_id"] == registration.key_id

    # The object rows were rebuilt, so nothing is owned: inventory is cleared.
    owned = _query(path, "SELECT * FROM objects WHERE owner_id IS NOT NULL")
    assert owned == []
    coin = _one(path, "SELECT * FROM objects WHERE id = 'gold_coin'")
    assert coin["room_id"] == "market"


def test_reset_says_it_is_destructive_and_that_inventory_is_cleared(
    tmp_path, capsys
):
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path)]) == 0

    conn = open_database(path)
    try:
        room = SimpleNamespace(
            item_id="gate",
            name="Village Gate",
            description="A heavy stone gate stands tall.",
            dark=False,
        )
        register_character(conn, "Ada", start_room=room)
    finally:
        conn.close()

    capsys.readouterr()
    assert main(["seed", "--reset", "--yes", "--db", str(path)]) == 0

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "destructive" in output
    assert "kept 1 character" in output
    assert "inventory cleared" in output


# ---------------------------------------------------------------------------
# Locks, one-way exits, NPCs, and authored object state
# ---------------------------------------------------------------------------


def test_seed_persists_locked_exits_npcs_and_author_state(tmp_path):
    world = _write_custom_world(tmp_path / "locked.yaml")
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path), "--world", str(world)]) == 0

    north = _one(
        path,
        "SELECT * FROM exits WHERE room_id = 'hall' AND direction = 'north'",
    )
    assert north["target_id"] == "vault"
    assert north["locked"] == 1
    assert north["key_id"] == "brass_key"
    assert north["label"] == "iron door"

    # A door that locks at one end locks at the other.
    south = _one(
        path,
        "SELECT * FROM exits WHERE room_id = 'vault' AND direction = 'south'",
    )
    assert south["target_id"] == "hall"
    assert south["locked"] == 1
    assert south["key_id"] == "brass_key"

    down = _one(
        path, "SELECT * FROM exits WHERE room_id = 'hall' AND direction = 'down'"
    )
    assert down["target_id"] == "cellar"
    assert _one(
        path,
        "SELECT * FROM exits WHERE room_id = 'cellar' AND direction = 'up'",
    ) is None

    npc = _one(path, "SELECT * FROM npcs WHERE id = 'keeper'")
    assert npc["room_id"] == "cellar"
    assert npc["wander"] == 1
    assert npc["wander_every"] == 5
    dialogue = json.loads(npc["dialogue"])
    assert dialogue["greeting"] == "Welcome, traveller."
    assert dialogue["topics"]["vault"] == "The vault is north of the hall."

    chest = json.loads(
        _one(path, "SELECT state FROM objects WHERE id = 'chest'")["state"]
    )
    assert chest["locked"] is True
    assert chest["required_key"] == "brass_key"
    assert chest["loot_tier"] == 2


# ---------------------------------------------------------------------------
# The v2 server reports the seeded epoch
# ---------------------------------------------------------------------------


def test_read_world_epoch_is_none_before_a_seed(tmp_path):
    conn = open_database(tmp_path / "empty.db")
    try:
        assert read_world_epoch(conn) is None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_v2_hello_and_session_ok_carry_the_seeded_epoch(tmp_path):
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path)]) == 0
    epoch = _meta(path)["epoch"]

    engine = Engine()
    await load_demo_village(engine)
    server = V2Server(engine, port=0, db_path=path)
    task = asyncio.create_task(server.run())
    await _wait_for_bind(server)
    try:
        async with websockets.connect(
            f"ws://localhost:{server.port}"
        ) as ws:
            hello = json.loads(await ws.recv())
            assert hello["type"] == "hello"
            assert hello["data"]["world"]["epoch"] == epoch

            await ws.send(json.dumps({
                "v": 2,
                "type": "join",
                "id": "join",
                "data": {"name": "Ada"},
            }))
            while True:
                message = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=3.0)
                )
                if message["type"] == "session.ok":
                    assert message["data"]["world"]["epoch"] == epoch
                    break
    finally:
        server.stop()
        await asyncio.wait_for(task, timeout=3.0)


@pytest.mark.asyncio
async def test_v2_auth_ok_carries_the_seeded_epoch(tmp_path):
    path = tmp_path / "mud.db"
    assert main(["seed", "--db", str(path)]) == 0
    epoch = _meta(path)["epoch"]

    engine = Engine()
    await load_demo_village(engine)
    server = V2Server(
        engine, port=0, db_path=path, auth_required=True)
    task = asyncio.create_task(server.run())
    await _wait_for_bind(server)
    try:
        async with websockets.connect(
            f"ws://localhost:{server.port}"
        ) as ws:
            hello = json.loads(await ws.recv())
            assert hello["data"]["world"]["epoch"] == epoch

            await ws.send(json.dumps({
                "v": 2,
                "type": "auth",
                "id": "register",
                "data": {"mode": "register", "name": "Ada"},
            }))
            while True:
                message = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=3.0)
                )
                if message["type"] == "auth.ok":
                    assert message["data"]["world"]["epoch"] == epoch
                    break
    finally:
        server.stop()
        await asyncio.wait_for(task, timeout=3.0)


def test_v2_server_without_a_database_uses_utc_now():
    server = V2Server(Engine(), port=0)

    assert datetime.fromisoformat(server.epoch)
