"""Compile a validated world model into one effective world value.

`load_world` in `models.py` answers "is this YAML shaped like a world?" and
`validate_world` answers "does it hold together?". This module does the
construction both consumers used to repeat: the reciprocal exits, the shared
lock identity of a two-way door, the containment placements, and the normalized
room geometry.

The engine loader installs entities from this value and the SQLite seeder writes
rows from it, so the world a player walks and the world the database records are
built once, from the same source (#132).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.mud.content.models import (
    NPC,
    OPPOSITE_EXITS,
    Exit,
    GameObject,
    LoadedWorld,
)
from src.mud.world.geometry import validate_geometry


@dataclass(frozen=True)
class CompiledLock:
    """One physical lock, shared by both directions of a two-way door."""

    key: str | None
    label: str | None


@dataclass(frozen=True)
class CompiledExit:
    """One effective edge: a declared exit or the reciprocal it creates.

    `lock_id` is None for an open edge; otherwise it names the shared
    `CompiledLock` in `CompiledWorld.locks`, so both sides of a door point at the
    same physical lock (and the same label).
    """

    room_id: str
    direction: str
    target_id: str
    lock_id: str | None


@dataclass(frozen=True)
class CompiledRoom:
    """A room with its authored content and normalized geometry."""

    id: str
    name: str
    description: str
    area: str | None
    dark: bool
    geometry: dict | None
    objects: tuple[GameObject, ...]
    npcs: tuple[NPC, ...]
    effects: dict


@dataclass(frozen=True)
class CompiledWorld:
    """The effective world: rooms, one exit per side, shared locks, placements."""

    name: str
    start_room: str
    rng_seed: int
    rooms: tuple[CompiledRoom, ...]
    exits: tuple[CompiledExit, ...]
    locks: dict[str, CompiledLock]
    # child object id -> the container object id that holds it
    contained: dict[str, str]

    def room(self, room_id: str) -> CompiledRoom:
        """The room with this id; raises KeyError if the id is unknown."""
        return next(room for room in self.rooms if room.id == room_id)

    def exit_map(self) -> dict[str, dict[str, str]]:
        """`room_id -> {direction: target_id}`, the shape geometry wants."""
        maps: dict[str, dict[str, str]] = {}
        for edge in self.exits:
            maps.setdefault(edge.room_id, {})[edge.direction] = edge.target_id
        return maps

    def top_level_objects(self, room_id: str) -> tuple[GameObject, ...]:
        """The room's objects that are not held inside a container."""
        return tuple(
            obj for obj in self.room(room_id).objects
            if obj.id not in self.contained
        )


def compile_world(loaded: LoadedWorld) -> CompiledWorld:
    """Expand a validated world into its one effective value."""
    world = loaded.world

    # 1. Exits. A declared edge creates its reciprocal unless `one_way`; a
    # locked door is one `CompiledLock` shared by both sides, label included.
    effective: dict[tuple[str, str], CompiledExit] = {}
    locks: dict[str, CompiledLock] = {}
    for room in world.rooms:
        for direction, value in room.exits.items():
            if isinstance(value, Exit):
                target_id = value.to
                one_way = value.one_way
                lock_id = None
                if value.locked:
                    lock_id = f"{room.id}:{direction}"
                    locks[lock_id] = CompiledLock(
                        key=value.key, label=value.label)
            else:
                target_id = value
                one_way = False
                lock_id = None

            effective[(room.id, direction)] = CompiledExit(
                room.id, direction, target_id, lock_id)

            opposite = OPPOSITE_EXITS.get(direction)
            if not one_way and opposite is not None:
                effective.setdefault(
                    (target_id, opposite),
                    CompiledExit(target_id, opposite, room.id, lock_id),
                )

    # 2. Geometry, validated once against the effective exits.
    exit_maps: dict[str, dict[str, str]] = {}
    for (room_id, direction), edge in effective.items():
        exit_maps.setdefault(room_id, {})[direction] = edge.target_id

    rooms: list[CompiledRoom] = []
    for room in world.rooms:
        geometry = None
        if room.geometry is not None:
            geometry = validate_geometry(
                room.id, room.geometry, exit_maps.get(room.id, {}))
        rooms.append(CompiledRoom(
            id=room.id,
            name=room.name,
            description=room.description,
            area=room.area,
            dark=room.dark,
            geometry=geometry,
            objects=tuple(room.objects),
            npcs=tuple(room.npcs),
            effects=dict(room.effects),
        ))

    # 3. Containment placements: a child belongs to its container, not its room.
    contained: dict[str, str] = {}
    for room in world.rooms:
        for obj in room.objects:
            for child in obj.contains:
                contained[child] = obj.id

    return CompiledWorld(
        name=world.name,
        start_room=world.start_room,
        rng_seed=world.rng_seed,
        rooms=tuple(rooms),
        exits=tuple(effective.values()),
        locks=locks,
        contained=contained,
    )
