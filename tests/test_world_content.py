"""Ticket #80: the v2.1.3 product world.

The world a player actually walks has 30-40 rooms, all reachable from
`start_room`, with at least five scripted NPCs and ten interactive objects
across containers, readables, keys, and locked exits. The five rooms of the
original starting village stay exactly as they were (one new south door on the
gate), and every area is reachable without a lock.

The JSON oracle (`starting_village.json` and its YAML twin) stays useful only
as the frozen five-room conformance fixture: `tests/test_world_yaml.py` still
proves those two files build the same engine graph. This file proves the
*product* world preserves that content while growing around it.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import yaml

from src.mud.content.models import (
    OPPOSITE_EXITS,
    Exit,
    GameObject,
    Room,
    load_world,
)
from src.mud.content.validator import check_world, validate_world
from src.mud.storage.db import open_database
from src.mud.world.seed import seed_world

REPO_ROOT = Path(__file__).resolve().parents[1]
WORLD_DIR = REPO_ROOT / "data" / "worlds" / "club_world"
LEGACY_WORLD = REPO_ROOT / "data" / "worlds" / "starting_village.yaml"

# One short walk from the gate into each area. Every hop is an unlocked exit,
# so a player can touch every area on day one; keys only open side content.
_GATE = "gate"
_SOUTH = ["gate", "south_road"]
_MILL = [*_SOUTH, "mill_bridge"]
_RIVER = [*_MILL, "river_path"]
_FERRY = [*_RIVER, "reeds", "ferry_landing", "ferry", "north_bank"]
_RUINS = [*_FERRY, "shrine", "ruins_gate", "ruins_court", "chapel"]

AREA_PATHS: dict[str, list[str]] = {
    "starting_village": [_GATE, "market", "shop"],
    "south_road": [*_SOUTH, "orchard", "beehives"],
    "mill": [*_MILL, "mill", "granary"],
    "riverside": [*_RIVER, "riverbank", "ford"],
    "north_bank": _FERRY,
    "ruins": _RUINS,
    "caves": [*_RUINS, "crypt", "cave_mouth", "cave_tunnel", "crystal_grotto"],
}

# The five rooms and objects the starting village already shipped. The product
# world keeps them; only the gate gains a south door.
STARTING_VILLAGE_ROOMS = ("gate", "market", "tavern", "shop", "forest")


class _Edge:
    """One effective exit, including the reciprocal the loader creates."""

    def __init__(
        self,
        source: str,
        direction: str,
        target: str,
        locked: bool = False,
        key: str | None = None,
    ):
        self.source = source
        self.direction = direction
        self.target = target
        self.locked = locked
        self.key = key


def _loaded():
    loaded = load_world(WORLD_DIR)
    assert validate_world(loaded) == []
    return loaded


def _edges(rooms: list[Room]) -> dict[tuple[str, str], _Edge]:
    """The effective exit map: declared edges plus loader reciprocals."""
    edges: dict[tuple[str, str], _Edge] = {}
    for room in rooms:
        for direction, value in room.exits.items():
            if isinstance(value, Exit):
                target = value.to
                one_way = value.one_way
                locked = value.locked
                key = value.key
            else:
                target = value
                one_way = False
                locked = False
                key = None

            edges[(room.id, direction)] = _Edge(
                room.id, direction, target, locked, key
            )
            opposite = OPPOSITE_EXITS.get(direction)
            if not one_way and opposite is not None:
                edges.setdefault(
                    (target, opposite),
                    _Edge(target, opposite, room.id, locked, key),
                )
    return edges


def _objects(rooms: list[Room]) -> dict[str, GameObject]:
    return {
        obj.id: obj for room in rooms for obj in room.objects
    }


def _object_room(rooms: list[Room]) -> dict[str, str]:
    return {
        obj.id: room.id for room in rooms for obj in room.objects
    }


def _contained_by(rooms: list[Room]) -> dict[str, str]:
    return {
        child: obj.id
        for room in rooms
        for obj in room.objects
        for child in obj.contains
    }


def _key_ids(rooms: list[Room]) -> set[str]:
    """Every object some lock names, whether an exit or a container."""
    keys: set[str] = set()
    for room in rooms:
        for value in room.exits.values():
            if isinstance(value, Exit) and value.locked and value.key:
                keys.add(value.key)
        for obj in room.objects:
            if obj.locked and obj.required_key:
                keys.add(obj.required_key)
    return keys


# ---------------------------------------------------------------------------
# Shape and reachability
# ---------------------------------------------------------------------------


def test_world_is_a_directory_with_metadata_and_area_files():
    assert WORLD_DIR.is_dir(), f"missing product world at {WORLD_DIR}"
    metadata = yaml.safe_load(
        (WORLD_DIR / "world.yaml").read_text(encoding="utf-8")
    )
    assert metadata["start_room"] == "gate"
    assert isinstance(metadata["rng_seed"], int)
    assert "rooms" not in metadata

    area_files = sorted((WORLD_DIR / "areas").glob("*.yaml"))
    assert {path.stem for path in area_files} == set(AREA_PATHS)


def test_world_has_30_to_40_rooms():
    loaded = _loaded()
    assert 30 <= len(loaded.world.rooms) <= 40


def test_riverside_loop_closes_on_the_grid():
    """Steps around the riverside loop must sum to zero (issue #140).

    A square grid cannot close a loop of odd length, so the five-exit loop
    (River Path, Riverbank, The Ford, Heron Rock, The Reeds) cannot be drawn
    without stretching an edge. The Shallows make it six steps.
    """

    loaded = _loaded()
    rooms = {room.id for room in loaded.world.rooms}
    assert "river_shallows" in rooms
    edges = _edges(loaded.world.rooms)
    steps = {
        "north": (0, -1),
        "south": (0, 1),
        "east": (1, 0),
        "west": (-1, 0),
    }
    loop = ["river_path", "riverbank", "ford", "river_shallows", "heron_rock", "reeds"]
    x = y = 0
    for source, target in pairwise([*loop, loop[0]]):
        direction = next(
            d for (src, d), edge in edges.items()
            if src == source and edge.target == target
        )
        dx, dy = steps[direction]
        x, y = x + dx, y + dy
    assert (x, y) == (0, 0)


def test_start_room_exists():
    loaded = _loaded()
    room_ids = {room.id for room in loaded.world.rooms}
    assert loaded.world.start_room in room_ids


def test_every_room_is_reachable_from_start():
    loaded = _loaded()
    rooms = loaded.world.rooms
    edges = _edges(rooms)

    start = loaded.world.start_room
    reachable = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        for (source, _), edge in edges.items():
            if source != current or edge.target in reachable:
                continue
            reachable.add(edge.target)
            stack.append(edge.target)

    missing = sorted({room.id for room in rooms} - reachable)
    assert missing == [], f"unreachable rooms: {missing}"


def test_every_exit_is_reciprocal_and_every_room_has_one():
    loaded = _loaded()
    edges = _edges(loaded.world.rooms)
    problems: list[str] = []

    for room in loaded.world.rooms:
        if not any(source == room.id for source, _ in edges):
            problems.append(f"{room.id} has no exits")

    for (source, direction), edge in edges.items():
        opposite = OPPOSITE_EXITS.get(direction)
        reverse = edges.get((edge.target, opposite)) if opposite else None
        if reverse is None or reverse.target != source:
            problems.append(
                f"{source} {direction} -> {edge.target} has no reciprocal "
                f"{opposite}"
            )

    # This world has no one-way traps; a later ticket may add a deliberate
    # one-way, but it must prove the player can still get home.
    for room in loaded.world.rooms:
        for direction, value in room.exits.items():
            if isinstance(value, Exit) and value.one_way:
                problems.append(f"{room.id} {direction} is one-way")

    assert problems == []


def test_every_area_has_a_golden_path():
    loaded = _loaded()
    rooms = loaded.world.rooms
    by_id = {room.id: room for room in rooms}
    edges = _edges(rooms)

    for area, path in AREA_PATHS.items():
        assert path[0] == loaded.world.start_room, area
        assert path[0] in by_id, (area, path[0])
        assert by_id[path[-1]].area == area, (area, path[-1])

        for source, target in pairwise(path):
            assert source in by_id and target in by_id
            forward = next(
                (
                    edge
                    for (room_id, _), edge in edges.items()
                    if room_id == source and edge.target == target
                ),
                None,
            )
            backward = next(
                (
                    edge
                    for (room_id, _), edge in edges.items()
                    if room_id == target and edge.target == source
                ),
                None,
            )
            edge = forward or backward
            assert edge is not None, f"{area}: no exit {source} <-> {target}"
            assert not edge.locked, (
                f"{area}: golden path crosses locked {source} -> {target}"
            )


def test_every_room_declares_its_area():
    loaded = _loaded()
    areas = {room.area for room in loaded.world.rooms}
    assert areas == set(AREA_PATHS)
    for room, path in zip(loaded.world.rooms, loaded.room_files):
        assert room.area is not None, room.id
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert document["area"] == room.area, (path, room.id)


# ---------------------------------------------------------------------------
# NPCs and interactive objects
# ---------------------------------------------------------------------------


def test_world_has_at_least_five_npcs_with_dialogue():
    loaded = _loaded()
    npcs = [npc for room in loaded.world.rooms for npc in room.npcs]

    assert len(npcs) >= 5
    for npc in npcs:
        assert npc.name
        assert npc.description
        assert npc.dialogue.greeting, npc.id
        assert npc.dialogue.default, npc.id
        assert npc.dialogue.topics, npc.id

    assert any(npc.wander for npc in npcs), "no NPC is authored to wander"


def test_world_has_ten_interactive_objects_across_four_kinds():
    loaded = _loaded()
    rooms = loaded.world.rooms
    objects = _objects(rooms)
    keys = _key_ids(rooms)

    containers = [obj for obj in objects.values() if obj.kind == "container"]
    readables = [obj for obj in objects.values() if obj.kind == "readable"]
    locked_exits = [
        (room.id, direction)
        for room in rooms
        for direction, value in room.exits.items()
        if isinstance(value, Exit) and value.locked
    ]

    assert len(containers) >= 1
    assert len(readables) >= 1
    assert len(keys) >= 1
    assert len(locked_exits) >= 1
    assert len(containers) + len(readables) + len(keys) + len(
        locked_exits
    ) >= 10

    for key_id in keys:
        assert key_id in objects, f"lock names missing key {key_id!r}"
        assert objects[key_id].portable, f"key {key_id!r} must be portable"


def test_content_check_is_clean():
    assert check_world(WORLD_DIR) == []


def test_effect_and_sleep_seams_have_content():
    """#84 reads effects and #72 drops authored objects; the world supplies both."""
    loaded = _loaded()
    rooms = loaded.world.rooms

    has_effect = any(room.effects for room in rooms)
    has_effect = has_effect or any(
        obj.effects for room in rooms for obj in room.objects
    )
    has_effect = has_effect or any(
        npc.effects for room in rooms for npc in room.npcs
    )
    assert has_effect

    assert any(
        obj.dropped_on_sleep for room in rooms for obj in room.objects
    )


# ---------------------------------------------------------------------------
# Locked content is reachable: no key sits behind its own door
# ---------------------------------------------------------------------------


def test_keys_and_containers_never_lock_the_player_out():
    """Walk the progression: a key opens a door, its contents open the next.

    The simulation ignores authors' intent and asks the engine's question: from
    `start_room`, with every container closed and every locked exit shut, which
    rooms can a player reach by taking what it can already see? The whole world
    must fall out of that fixpoint.
    """
    loaded = _loaded()
    rooms = loaded.world.rooms
    edges = _edges(rooms)
    objects = _objects(rooms)
    object_room = _object_room(rooms)
    contained_by = _contained_by(rooms)

    reachable = {loaded.world.start_room}
    held: set[str] = set()
    opened: set[str] = set()

    def available(object_id: str) -> bool:
        parent = contained_by.get(object_id)
        if parent is not None:
            return parent in opened
        return object_room[object_id] in reachable

    changed = True
    while changed:
        changed = False

        for object_id, obj in objects.items():
            if object_id in held or not available(object_id):
                continue
            if obj.kind == "container":
                if obj.locked and obj.required_key not in held:
                    continue
                if object_id not in opened:
                    opened.add(object_id)
                    changed = True
            held.add(object_id)
            changed = True

        for (source, _), edge in edges.items():
            if source not in reachable:
                continue
            if edge.locked and edge.key not in held:
                continue
            if edge.target not in reachable:
                reachable.add(edge.target)
                changed = True

    missing = sorted({room.id for room in rooms} - reachable)
    assert missing == [], f"locked out of: {missing}"

    for key_id in _key_ids(rooms):
        assert key_id in held, f"key {key_id!r} is not obtainable"


# ---------------------------------------------------------------------------
# The five original rooms survive
# ---------------------------------------------------------------------------


def _legacy_rooms() -> dict[str, dict]:
    document = yaml.safe_load(LEGACY_WORLD.read_text(encoding="utf-8"))
    return {room["id"]: room for room in document["rooms"]}


def test_starting_village_is_preserved_in_the_product_world():
    legacy = _legacy_rooms()
    loaded = _loaded()
    product = {room.id: room for room in loaded.world.rooms}

    assert set(legacy) == set(STARTING_VILLAGE_ROOMS)
    for room_id in STARTING_VILLAGE_ROOMS:
        old = legacy[room_id]
        new = product[room_id]

        assert new.name == old["name"], room_id
        assert new.description == old["description"], room_id

        # The gate gains one south door into the new world; nothing else moves.
        extra = {"south"} if room_id == "gate" else set()
        assert set(new.exits) == set(old.get("exits", {})) | extra, room_id
        for direction, target in old.get("exits", {}).items():
            value = new.exits[direction]
            target_id = value.to if isinstance(value, Exit) else value
            old_target = target["to"] if isinstance(target, dict) else target
            assert target_id == old_target, (room_id, direction)
            if isinstance(target, dict):
                assert isinstance(value, Exit), (room_id, direction)
                assert value.locked == target.get("locked", False)
                assert value.key == target.get("key")

        if "geometry" in old:
            expected = old["geometry"]
            if room_id == "gate":
                expected = {
                    **expected,
                    "cells": [
                        *expected["cells"][:-1],
                        "###+###",
                    ],
                }
            assert new.geometry == expected, room_id

        product_objects = {
            obj.id: obj for obj in new.objects
        }
        defaults = {
            "kind": "item",
            "portable": True,
            "weight": 0.0,
            "readable_text": None,
            "locked": False,
            "required_key": None,
            "light_source": False,
            "lit": False,
            "dropped_on_sleep": False,
            "state": {},
            "contains": [],
        }
        for old_object in old.get("objects", []):
            new_object = product_objects.get(old_object["id"])
            assert new_object is not None, old_object["id"]
            for field in (
                "name",
                "description",
                "kind",
                "portable",
                "weight",
                "readable_text",
                "locked",
                "required_key",
                "light_source",
                "lit",
                "dropped_on_sleep",
                "state",
                "contains",
            ):
                old_value = old_object.get(field, defaults.get(field))
                assert getattr(new_object, field) == old_value, (
                    room_id,
                    old_object["id"],
                    field,
                )

    assert loaded.world.start_room == "gate"


# ---------------------------------------------------------------------------
# Seeding stores the whole world
# ---------------------------------------------------------------------------


def test_seed_world_writes_the_product_world(tmp_path):
    loaded = _loaded()
    rooms = loaded.world.rooms
    conn = open_database(tmp_path / "mud.db")
    try:
        result = seed_world(conn, WORLD_DIR, epoch="test-epoch")
        counts = {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in ("rooms", "objects", "npcs", "exits")
        }
    finally:
        conn.close()

    assert result.rooms == len(rooms)
    assert result.npcs == sum(len(room.npcs) for room in rooms)
    assert counts["rooms"] == len(rooms)
    assert counts["npcs"] == result.npcs
    assert counts["objects"] == sum(len(room.objects) for room in rooms)
