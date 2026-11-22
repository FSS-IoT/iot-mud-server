"""Seed the effective YAML world into SQLite (SPEC v2.1.0 criterion 3).

`admin seed` writes the world a player can actually walk through: every room
(with its `dark` flag and optional geometry), every effective exit (including
the reciprocal the loader would create), every object with its placement, every
NPC, and the `world_meta` row set. A reset replaces that world in one
transaction and changes `world_meta.epoch`.

**Fields the schema has no column for** — object weight, light flags, lock
flags, and authored `state` — go into `objects.state`, a JSON object. That
keeps the seeded row lossless enough for a later reload without a migration the
wire format does not need. NPC dialogue is stored as JSON in `npcs.dialogue`.
Room and NPC `effects` have no column yet; the v2.1.3 effects ticket owns that.

**A reset is destructive to the world, not to people.** `players` and
`character_keys` survive, every character (awake or asleep) is placed at the
new `start_room`, and every carried or owned object is gone with the old world,
so inventory is cleared. Characters are never deleted.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.mud.content.models import (
    OPPOSITE_EXITS,
    Exit,
    GameObject,
    World,
    load_world,
)
from src.mud.content.validator import validate_world
from src.mud.world.geometry import validate_geometry

# The tables that hold world content. `players` and `character_keys` are not in
# this list: a reset replaces the world and keeps the people.
WORLD_TABLES = ("exits", "objects", "npcs", "rooms", "world_meta")


class SeedError(RuntimeError):
    """The world could not be seeded. Nothing was written."""


class WorldAlreadySeeded(SeedError):
    """The database already holds a world; a reset is required to replace it."""


@dataclass(frozen=True)
class SeedResult:
    """What one seed wrote, for the CLI to report."""

    world_path: Path
    name: str
    start_room: str
    epoch: str
    rooms: int
    exits: int
    objects: int
    npcs: int
    characters: int
    reset: bool


def is_world_empty(conn: sqlite3.Connection) -> bool:
    """True when the database holds no world content yet.

    A database created by `admin init-db` is empty. A character registered
    against a fresh database creates its start room, so that database is no
    longer empty and seeding it requires an explicit reset.
    """
    for table in ("world_meta", "rooms", "objects", "npcs"):
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            return False
    return True


def _effective_exits(
    world: World,
) -> dict[tuple[str, str], tuple[str, bool, str | None, str | None]]:
    """The exits a player can use: declared edges plus loader reciprocals.

    A locked door locks from both sides: the reciprocal inherits `locked` and
    `key`. Its `label` is direction-specific ("iron door" faces the room that
    declared it), so the reciprocal keeps no label.
    """
    effective: dict[
        tuple[str, str], tuple[str, bool, str | None, str | None]
    ] = {}
    for room in world.rooms:
        for direction, value in room.exits.items():
            if isinstance(value, Exit):
                target_id = value.to
                one_way = value.one_way
                locked = value.locked
                key = value.key
                label = value.label
            else:
                target_id = value
                one_way = False
                locked = False
                key = None
                label = None

            effective[(room.id, direction)] = (target_id, locked, key, label)
            opposite = OPPOSITE_EXITS.get(direction)
            if not one_way and opposite is not None:
                effective.setdefault(
                    (target_id, opposite), (room.id, locked, key, None)
                )
    return effective


def _exit_maps(
    effective: dict[tuple[str, str], tuple[str, bool, str | None, str | None]],
) -> dict[str, dict[str, str]]:
    """Per-room `direction -> target`, the shape geometry validation wants."""
    maps: dict[str, dict[str, str]] = {}
    for (room_id, direction), (target_id, _, _, _) in effective.items():
        maps.setdefault(room_id, {})[direction] = target_id
    return maps


def _object_state(obj: GameObject) -> str:
    """The schema-less half of an object, as JSON.

    Authored `state` is merged with the model fields the schema does not carry.
    The model fields win, so an author cannot shadow `weight` or `locked` with
    an unrelated device value of the same name.
    """
    state: dict[str, Any] = dict(obj.state)
    state.update(
        {
            "weight": obj.weight,
            "light_source": obj.light_source,
            "lit": obj.lit,
            "locked": obj.locked,
            "required_key": obj.required_key,
            "dropped_on_sleep": obj.dropped_on_sleep,
            # Explicit lid state (#76); authored `state.open` wins only through
            # the dict above, so normalize it once for every object row.
            "open": bool(obj.state.get("open", False)),
        }
    )
    return json.dumps(state, sort_keys=True)


def _room_rows(
    world: World, exit_maps: dict[str, dict[str, str]]
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for room in world.rooms:
        geometry = None
        if room.geometry is not None:
            # Normalize the wire shape once; the validator has already rejected
            # a map the engine could not walk through.
            geometry = json.dumps(
                validate_geometry(
                    room.id, room.geometry, exit_maps.get(room.id, {})
                ),
                sort_keys=True,
            )
        rows.append(
            (
                room.id,
                room.name,
                room.description,
                room.area,
                1 if room.dark else 0,
                geometry,
            )
        )
    return rows


def _exit_rows(
    effective: dict[tuple[str, str], tuple[str, bool, str | None, str | None]],
) -> list[tuple[Any, ...]]:
    return [
        (room_id, direction, target_id, 1 if locked else 0, key, label)
        for (room_id, direction), (target_id, locked, key, label)
        in effective.items()
    ]


def _object_rows(world: World) -> list[tuple[Any, ...]]:
    contained = {
        child
        for room in world.rooms
        for obj in room.objects
        for child in obj.contains
    }
    # A contained object belongs to its container, not to the room its YAML
    # declares it in. `container_id` is filled in after every row exists so the
    # self-referencing foreign key is satisfied in insertion order.
    return [
        (
            obj.id,
            obj.name,
            obj.description,
            obj.kind,
            1 if obj.portable else 0,
            obj.readable_text,
            None if obj.id in contained else room.id,
            _object_state(obj),
        )
        for room in world.rooms
        for obj in room.objects
    ]


def _containment_rows(world: World) -> list[tuple[str, str]]:
    return [
        (child, obj.id)
        for room in world.rooms
        for obj in room.objects
        for child in obj.contains
    ]


def _npc_rows(world: World) -> list[tuple[Any, ...]]:
    return [
        (
            npc.id,
            npc.name,
            npc.description,
            room.id,
            json.dumps(
                {
                    "greeting": npc.dialogue.greeting,
                    "default": npc.dialogue.default,
                    "topics": dict(npc.dialogue.topics),
                },
                sort_keys=True,
            ),
            1 if npc.wander else 0,
            npc.wander_every,
        )
        for room in world.rooms
        for npc in room.npcs
    ]


def _insert_world(conn: sqlite3.Connection, world: World, epoch: str) -> None:
    effective = _effective_exits(world)

    conn.executemany(
        "INSERT INTO rooms(id, name, description, area, dark, geometry) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        _room_rows(world, _exit_maps(effective)),
    )
    # Objects come before exits: `exits.key_id` references an object.
    conn.executemany(
        "INSERT INTO objects(id, name, description, kind, portable, "
        "readable_text, room_id, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        _object_rows(world),
    )
    conn.executemany(
        "UPDATE objects SET container_id = ? WHERE id = ?",
        [(container, child) for child, container in _containment_rows(world)],
    )
    conn.executemany(
        "INSERT INTO exits(room_id, direction, target_id, locked, key_id, "
        "label) VALUES (?, ?, ?, ?, ?, ?)",
        _exit_rows(effective),
    )
    conn.executemany(
        "INSERT INTO npcs(id, name, description, room_id, dialogue, wander, "
        "wander_every) VALUES (?, ?, ?, ?, ?, ?, ?)",
        _npc_rows(world),
    )
    conn.executemany(
        "INSERT INTO world_meta(key, value) VALUES (?, ?)",
        [
            ("name", world.name),
            ("start_room", world.start_room),
            ("rng_seed", str(world.rng_seed)),
            ("epoch", epoch),
        ],
    )


def _clear_world(conn: sqlite3.Connection) -> None:
    """Delete the world. Players and character keys are untouched.

    `players.room_id` is declared `ON DELETE SET NULL`, so it is nulled while
    the rooms are gone and restored by `seed_world` in the same transaction.
    Existing object rows own the characters' inventories, so clearing them is
    what clears inventory.
    """
    for table in WORLD_TABLES:
        conn.execute(f"DELETE FROM {table}")


def _loaded_world(world_path: str | Path) -> World:
    loaded = load_world(world_path)
    errors = validate_world(loaded)
    if errors:
        raise SeedError("\n".join(errors))
    return loaded.world


def seed_world(
    conn: sqlite3.Connection,
    world_path: str | Path,
    *,
    reset: bool = False,
    epoch: str | None = None,
) -> SeedResult:
    """Write the YAML world into `conn`, or replace it when `reset`.

    A seed always runs in one transaction: either the whole world lands or the
    database is unchanged. Without `reset`, a database that already holds world
    content raises `WorldAlreadySeeded` instead of overwriting it.

    `epoch` is a test seam; production gets a fresh UUID every time.
    """
    if not reset and not is_world_empty(conn):
        raise WorldAlreadySeeded(
            "the database already holds a world; re-run with --reset --yes "
            "to replace it"
        )

    world = _loaded_world(world_path)
    new_epoch = epoch or str(uuid.uuid4())
    characters = 0

    with conn:
        if reset:
            _clear_world(conn)
        _insert_world(conn, world, new_epoch)

        characters = int(
            conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        )
        if characters:
            # The old rooms are gone and the new world has its own start room.
            # Every character — awake or asleep — starts there, and whatever it
            # carried went with the old world.
            conn.execute(
                "UPDATE players SET room_id = ?", (world.start_room,)
            )

    effective = _effective_exits(world)
    return SeedResult(
        world_path=Path(world_path),
        name=world.name,
        start_room=world.start_room,
        epoch=new_epoch,
        rooms=len(world.rooms),
        exits=len(effective),
        objects=sum(len(room.objects) for room in world.rooms),
        npcs=sum(len(room.npcs) for room in world.rooms),
        characters=characters,
        reset=reset,
    )
